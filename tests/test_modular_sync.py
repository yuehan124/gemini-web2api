import http.client
import base64
import json
import threading
import unittest
from unittest import mock
from urllib.parse import parse_qs

from gemini_web2api.config import CONFIG, DEFAULT_CONFIG
from gemini_web2api.gemini import _build_payload
from gemini_web2api.images import (
    classify_image_failure,
    extract_generated_videos,
    is_video_request,
)
from gemini_web2api.server import GeminiHandler, ThreadedServer
from gemini_web2api.tools import google_contents_to_prompt, messages_to_prompt


def _decode_payload(payload):
    outer = json.loads(parse_qs(payload)["f.req"][0])
    return json.loads(outer[1])


def _raw_response(text, images=None):
    """Build a minimal raw StreamGenerate response carrying `text`."""
    inner = [None, ["c_test", "r_test"], None, None, [["rc_test", [text]]]]
    if images:
        inner.append([images])
    arr = [["wrb.fr", None, json.dumps(inner)]]
    return json.dumps(arr).ljust(220)


def _media_node(kind="image", filename="image.png", mime="image/png"):
    return [
        None, 1, filename,
        f"https://lh3.googleusercontent.com/gg-dl/{kind}-token",
        None, f"{kind}-id", None, None, 2, [1, 2], None, mime,
        None, None, None, [1024, 1024, 123],
    ]


def _video_response(cid="c_0123456789abcdef", rid="r_fedcba9876543210"):
    inner = [
        None, [cid, rid], None, None, [["rc_test", ["Here is your video"]]],
        [_media_node("video", "clip.mp4", "video/mp4")],
    ]
    arr = [["wrb.fr", None, json.dumps(inner)]]
    return json.dumps(arr).ljust(220)


def _image_response(cid="c_0123456789abcdef", rid="r_fedcba9876543210"):
    inner = [
        None, [cid, rid], None, None, [["rc_test", ["Here is your image"]]],
        [_media_node()],
    ]
    arr = [["wrb.fr", None, json.dumps(inner)]]
    return json.dumps(arr).ljust(220)


def _decode_sse(body):
    events = []
    for block in body.strip().split("\n\n"):
        lines = block.splitlines()
        event_type = next(
            (line[len("event: "):] for line in lines if line.startswith("event: ")),
            None,
        )
        data = next(
            (line[len("data: "):] for line in lines if line.startswith("data: ")),
            None,
        )
        if event_type and data:
            events.append((event_type, json.loads(data)))
    return events


class PayloadPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.original_config = dict(CONFIG)

    def tearDown(self):
        CONFIG.clear()
        CONFIG.update(self.original_config)

    def test_temporary_chats_default_to_disabled(self):
        self.assertIs(DEFAULT_CONFIG["temporary_chats"], False)

    def test_persistent_chat_payload(self):
        CONFIG["temporary_chats"] = False

        inner = _decode_payload(_build_payload("hello", 1, 4))

        self.assertEqual(inner[41], [2])
        self.assertIsNone(inner[45])

    def test_temporary_chat_payload(self):
        CONFIG["temporary_chats"] = True

        inner = _decode_payload(_build_payload("hello", 1, 4))

        self.assertEqual(inner[41], [1])
        self.assertEqual(inner[45], 1)

    def test_payload_includes_uploaded_image_refs(self):
        inner = _decode_payload(_build_payload("describe", 1, 4, ["/uploaded/image-ref"]))

        self.assertEqual(inner[0][0], "describe")
        self.assertEqual(inner[0][3], [[None, None, "/uploaded/image-ref"]])

    def test_payload_includes_previous_chat_context(self):
        inner = _decode_payload(_build_payload(
            "next image", 1, 4,
            chat_context={"cid": "c_0123456789abcdef", "rid": "r_fedcba9876543210",
                          "rcid": "rc_0123456789abcdef"},
        ))

        # Gemini Web continues with [cid, rid]; rcid is not part of chat metadata.
        self.assertEqual(inner[2], ["c_0123456789abcdef", "r_fedcba9876543210"])


class MessageParsingTests(unittest.TestCase):
    def test_messages_to_prompt_extracts_openai_image_url_data_url(self):
        image_data = base64.b64encode(b"fake png").decode()

        prompt, images = messages_to_prompt([{
            "role": "user",
            "content": [
                {"type": "text", "text": "Describe"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_data}"}},
            ],
        }])

        self.assertEqual(prompt, "Describe [Image attached]")
        self.assertEqual(images, [(b"fake png", "image/png")])

    def test_messages_to_prompt_extracts_responses_input_image_url(self):
        prompt, images = messages_to_prompt([{
            "role": "user",
            "content": [
                {"type": "input_text", "text": "Describe"},
                {"type": "input_image", "image_url": "https://example.com/image.png"},
            ],
        }])

        self.assertEqual(prompt, "Describe [Image attached]")
        self.assertEqual(images, [("https://example.com/image.png", "image/png")])

    def test_messages_to_prompt_ignores_malformed_image_data_url(self):
        prompt, images = messages_to_prompt([{
            "role": "user",
            "content": [
                {"type": "text", "text": "Describe"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,%%%"}},
            ],
        }])

        self.assertEqual(prompt, "Describe")
        self.assertEqual(images, [])

    def test_google_contents_to_prompt_extracts_inline_image_data(self):
        image_data = base64.b64encode(b"fake png").decode()

        prompt, images = google_contents_to_prompt({
            "contents": [{
                "role": "user",
                "parts": [
                    {"text": "Describe"},
                    {"inlineData": {"mimeType": "image/png", "data": image_data}},
                ],
            }],
        })

        self.assertEqual(prompt, "Describe\n[Image attached]")
        self.assertEqual(images, [(b"fake png", "image/png")])

    def test_google_contents_to_prompt_ignores_malformed_inline_image_data(self):
        prompt, images = google_contents_to_prompt({
            "contents": [{
                "role": "user",
                "parts": [
                    {"text": "Describe"},
                    {"inlineData": {"mimeType": "image/png", "data": "%%%"}},
                ],
            }],
        })

        self.assertEqual(prompt, "Describe")
        self.assertEqual(images, [])


class ImageMediaDetectionTests(unittest.TestCase):
    def test_video_nodes_are_not_returned_as_images(self):
        from gemini_web2api.images import extract_generated_images

        self.assertEqual(extract_generated_images(_video_response()), [])

    def test_extracts_video_descriptor(self):
        videos = extract_generated_videos(_video_response())

        self.assertEqual(len(videos), 1)
        self.assertEqual(videos[0]["mime"], "video/mp4")
        self.assertEqual(videos[0]["filename"], "clip.mp4")
        self.assertEqual(videos[0]["cid"], "c_0123456789abcdef")

    def test_video_placeholder_is_detected_without_media_node(self):
        raw = json.dumps([["wrb.fr", None, json.dumps([
            None, ["c_test", "r_test"], None, None,
            [["rc_test", ["http://googleusercontent.com/video_generation_content/1_0"]]],
        ])]]).ljust(220)

        videos = extract_generated_videos(raw)

        self.assertEqual(len(videos), 1)
        self.assertIn("video_generation_content", videos[0]["url"])

    def test_classify_video_failure_returns_machine_readable_code(self):
        error = classify_image_failure(_video_response(), "Here is your video")

        self.assertEqual(error["code"], "gemini_video_response")
        self.assertEqual(error["type"], "upstream_unsupported_media")
        self.assertEqual(error["media_type"], "video/mp4")
        self.assertIn("video", error["message"])
        self.assertEqual(error["detail"], "Here is your video")

    def test_classify_text_only_failure_keeps_upstream_message(self):
        error = classify_image_failure(
            _raw_response("I can't create images right now"),
            "I can't create images right now",
        )

        self.assertEqual(error["code"], "gemini_text_response")
        self.assertEqual(error["message"], "I can't create images right now")

    def test_video_intent_detection(self):
        self.assertTrue(is_video_request("生成一段视频"))
        self.assertTrue(is_video_request("Generate a video of a cat"))
        self.assertTrue(is_video_request("text-to-video"))
        self.assertFalse(is_video_request("生成一张红苹果的图片"))
        self.assertFalse(is_video_request("生成视频封面图片"))
        self.assertFalse(is_video_request("不要生成视频，只要图片"))
        self.assertFalse(is_video_request("make a video thumbnail"))
        self.assertFalse(is_video_request("Generate a video game character"))
        self.assertFalse(is_video_request("生成视频游戏角色"))


class StreamingEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadedServer(("127.0.0.1", 0), GeminiHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        self.original_config = dict(CONFIG)
        CONFIG["api_keys"] = []
        CONFIG["log_requests"] = False

    def tearDown(self):
        CONFIG.clear()
        CONFIG.update(self.original_config)

    def post_json(self, path, payload):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(
            "POST",
            path,
            body=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        body = response.read().decode()
        headers = dict(response.getheaders())
        connection.close()
        return response.status, headers, body

    def post_chunked_json(self, path, payload):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(
            "POST",
            path,
            body=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            encode_chunked=True,
        )
        response = connection.getresponse()
        body = response.read().decode()
        headers = dict(response.getheaders())
        connection.close()
        return response.status, headers, body

    def test_image_generation_uses_gemini_conversation_id_and_reuses_context(self):
        cid = "c_0123456789abcdef"
        generated = {
            "url": "https://lh3.googleusercontent.com/image.png",
            "cid": cid,
            "rid": "r_fedcba9876543210",
            "rcid": "rc_0123456789abcdef",
        }
        with mock.patch("gemini_web2api.server.generate_raw", return_value="raw") as generate, \
                mock.patch("gemini_web2api.server.extract_generated_images", return_value=[generated]):
            status, _, body = self.post_json(
                "/v1/images/generations",
                {"model": "gemini-3.6-flash", "prompt": "Generate the first frame"},
            )
            self.assertEqual(status, 200, body)
            self.assertEqual(json.loads(body)["session_id"], cid)

            status, _, body = self.post_json(
                "/v1/images/generations",
                {"model": "gemini-3.6-flash", "prompt": "Generate the next frame",
                 "session_id": cid},
            )
            self.assertEqual(status, 200, body)
            self.assertEqual(json.loads(body)["session_id"], cid)

            status, _, body = self.post_json(
                "/v1/images/generations",
                {"model": "gemini-3.6-flash", "prompt": "Generate the next frame",
                 "session_id": "c_fedcba9876543210"},
            )
            self.assertEqual(status, 404)
            self.assertEqual(json.loads(body)["error"]["message"], "unknown or expired session_id")

        self.assertIsNone(generate.call_args_list[0].kwargs.get("chat_context"))
        self.assertEqual(
            generate.call_args_list[1].kwargs["chat_context"],
            {"cid": cid, "rid": "r_fedcba9876543210"},
        )

    def test_image_generation_retries_after_video_response(self):
        with mock.patch(
            "gemini_web2api.server.generate_raw",
            side_effect=[_video_response(), _image_response()],
        ) as generate:
            status, _, body = self.post_json(
                "/v1/images/generations",
                {"model": "gemini-3.6-flash", "prompt": "Draw a red apple"},
            )

        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["session_id"], "c_0123456789abcdef")
        self.assertEqual(generate.call_count, 2)
        self.assertIn("still image", generate.call_args_list[1].args[0].lower())

    def test_image_generation_returns_video_error_when_retry_also_returns_video(self):
        with mock.patch(
            "gemini_web2api.server.generate_raw",
            side_effect=[_video_response(), _video_response()],
        ) as generate:
            status, _, body = self.post_json(
                "/v1/images/generations",
                {"model": "gemini-3.6-flash", "prompt": "Draw a red apple"},
            )

        self.assertEqual(status, 502)
        error = json.loads(body)["error"]
        self.assertEqual(error["code"], "gemini_video_response")
        self.assertEqual(error["media_type"], "video/mp4")
        self.assertIn("video", error["message"])
        self.assertEqual(generate.call_count, 2)

    def test_image_generation_rejects_explicit_video_prompt(self):
        with mock.patch("gemini_web2api.server.generate_raw") as generate:
            status, _, body = self.post_json(
                "/v1/images/generations",
                {"model": "gemini-3.6-flash", "prompt": "生成一段视频"},
            )

        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], "video_not_supported")
        generate.assert_not_called()

    def test_image_generation_rejects_invalid_session_ids(self):
        for session_id in ("bad/session", "batch-a", "c_tooshort"):
            status, _, body = self.post_json(
                "/v1/images/generations",
                {"model": "gemini-3.6-flash", "prompt": "Generate an image",
                 "session_id": session_id},
            )

            self.assertEqual(status, 400)
            self.assertIn("invalid session_id", json.loads(body)["error"]["message"])

    @mock.patch("gemini_web2api.server.generate_stream")
    def test_chat_stream_starts_with_assistant_role(self, generate_stream):
        generate_stream.return_value = iter(["hel", "lo"])

        status, headers, body = self.post_json(
            "/v1/chat/completions",
            {
                "model": "gemini-3.6-flash",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
            },
        )

        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/event-stream")
        chunks = [
            json.loads(line[len("data: "):])
            for line in body.splitlines()
            if line.startswith("data: {")
        ]
        self.assertEqual(chunks[0]["choices"][0]["delta"], {"role": "assistant"})
        self.assertEqual(chunks[1]["choices"][0]["delta"], {"content": "hel"})
        self.assertEqual(chunks[2]["choices"][0]["delta"], {"content": "lo"})
        self.assertTrue(body.endswith("data: [DONE]\n\n"))

    @mock.patch("gemini_web2api.server.generate_raw", return_value=_raw_response("chunked ok"))
    def test_chat_accepts_chunked_body(self, _generate):
        status, _, body = self.post_chunked_json(
            "/v1/chat/completions",
            {
                "model": "gemini-3.6-flash",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["choices"][0]["message"]["content"], "chunked ok")

    @mock.patch("gemini_web2api.server.upload_image", return_value="/uploaded/image-ref")
    @mock.patch("gemini_web2api.server.generate_raw", return_value=_raw_response("looks good"))
    def test_chat_accepts_openai_image_url_data_url(self, generate, upload_image):
        image_data = base64.b64encode(b"fake png").decode()

        status, _, body = self.post_json(
            "/v1/chat/completions",
            {
                "model": "gemini-3.6-flash",
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Describe this image"},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{image_data}"
                            },
                        },
                    ],
                }],
            },
        )

        self.assertEqual(status, 200)
        upload_image.assert_called_once_with(b"fake png", "image.png", "image/png")
        self.assertEqual(generate.call_args.args[3], ["/uploaded/image-ref"])
        self.assertIn("[Image attached]", generate.call_args.args[0])
        self.assertEqual(json.loads(body)["choices"][0]["message"]["content"], "looks good")

    @mock.patch("gemini_web2api.server.fetch_image_bytes", return_value=b"\xff\xd8\xffremote jpeg")
    @mock.patch("gemini_web2api.server.upload_image", return_value="/uploaded/remote-ref")
    @mock.patch("gemini_web2api.server.generate_raw", return_value=_raw_response("remote ok"))
    def test_responses_accepts_input_image_url(self, generate, upload_image, fetch_image_bytes):
        status, _, _ = self.post_json(
            "/v1/responses",
            {
                "model": "gemini-3.6-flash",
                "input": [{
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "What is shown?"},
                        {
                            "type": "input_image",
                            "image_url": "https://example.com/image.jpg",
                        },
                    ],
                }],
            },
        )

        self.assertEqual(status, 200)
        fetch_image_bytes.assert_called_once_with("https://example.com/image.jpg")
        upload_image.assert_called_once_with(b"\xff\xd8\xffremote jpeg", "image.png", "image/jpeg")
        self.assertEqual(generate.call_args.args[3], ["/uploaded/remote-ref"])
        self.assertIn("[Image attached]", generate.call_args.args[0])

    @mock.patch("gemini_web2api.server.upload_image", return_value="/uploaded/image-ref")
    @mock.patch("gemini_web2api.server.generate_raw", return_value=_raw_response("top-level image ok"))
    def test_responses_accepts_top_level_input_image(self, generate, upload_image):
        image_data = base64.b64encode(b"fake png").decode()

        status, _, _ = self.post_json(
            "/v1/responses",
            {
                "model": "gemini-3.6-flash",
                "input": [
                    {"type": "input_text", "text": "What is shown?"},
                    {
                        "type": "input_image",
                        "image_url": f"data:image/png;base64,{image_data}",
                    },
                ],
            },
        )

        self.assertEqual(status, 200)
        upload_image.assert_called_once_with(b"fake png", "image.png", "image/png")
        self.assertEqual(generate.call_args.args[3], ["/uploaded/image-ref"])
        self.assertIn("What is shown?", generate.call_args.args[0])
        self.assertIn("[Image attached]", generate.call_args.args[0])

    @mock.patch("gemini_web2api.server.upload_image", side_effect=RuntimeError("upload denied"))
    def test_google_image_upload_failure_returns_502(self, _upload_image):
        image_data = base64.b64encode(b"fake png").decode()

        status, _, body = self.post_json(
            "/v1beta/models/gemini-3.6-flash:generateContent",
            {
                "contents": [{
                    "role": "user",
                    "parts": [{
                        "inlineData": {
                            "mimeType": "image/png",
                            "data": image_data,
                        },
                    }],
                }],
            },
        )

        self.assertEqual(status, 502)
        self.assertIn("image upload failed: upload denied", json.loads(body)["error"]["message"])

    @mock.patch("gemini_web2api.server.generate_stream", return_value=iter(["streamed"]))
    def test_google_stream_generate_content_uses_sse(self, _generate_stream):
        status, headers, body = self.post_json(
            "/v1beta/models/gemini-3.6-flash:streamGenerateContent",
            {
                "contents": [{
                    "role": "user",
                    "parts": [{"text": "Stream this"}],
                }],
            },
        )

        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/event-stream")
        self.assertIn('"text": "streamed"', body)

    @mock.patch("gemini_web2api.server.generate_raw", return_value=_raw_response("hello"))
    def test_responses_text_stream_has_complete_event_sequence(self, _generate):
        status, headers, body = self.post_json(
            "/v1/responses",
            {
                "model": "gemini-3.6-flash",
                "input": "hello",
                "stream": True,
            },
        )

        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/event-stream")
        events = _decode_sse(body)
        self.assertEqual(
            [event_type for event_type, _ in events],
            [
                "response.created",
                "response.in_progress",
                "response.output_item.added",
                "response.content_part.added",
                "response.output_text.delta",
                "response.output_text.done",
                "response.content_part.done",
                "response.output_item.done",
                "response.completed",
            ],
        )
        self.assertEqual(
            [event["sequence_number"] for _, event in events],
            list(range(1, len(events) + 1)),
        )
        self.assertEqual(events[4][1]["delta"], "hello")
        self.assertEqual(events[-1][1]["response"]["status"], "completed")
        self.assertEqual(events[-1][1]["response"]["output"][0]["content"][0]["text"], "hello")

    @mock.patch("gemini_web2api.server.parse_tool_calls")
    @mock.patch("gemini_web2api.server.generate_raw", return_value=_raw_response("tool output"))
    def test_responses_function_call_stream_has_complete_event_sequence(
        self, _generate, parse_tool_calls
    ):
        parse_tool_calls.return_value = (
            "",
            [
                {
                    "id": "call_test",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city":"Shanghai"}'},
                }
            ],
        )

        status, _, body = self.post_json(
            "/v1/responses",
            {
                "model": "gemini-3.6-flash",
                "input": "weather",
                "tools": [
                    {
                        "type": "function",
                        "name": "get_weather",
                        "description": "Get weather",
                        "parameters": {"type": "object"},
                    }
                ],
                "stream": True,
            },
        )

        self.assertEqual(status, 200)
        events = _decode_sse(body)
        self.assertEqual(
            [event_type for event_type, _ in events],
            [
                "response.created",
                "response.in_progress",
                "response.output_item.added",
                "response.function_call_arguments.delta",
                "response.function_call_arguments.done",
                "response.output_item.done",
                "response.completed",
            ],
        )
        self.assertEqual(
            [event["sequence_number"] for _, event in events],
            list(range(1, len(events) + 1)),
        )
        self.assertEqual(events[2][1]["output_index"], 0)
        self.assertEqual(events[3][1]["delta"], '{"city":"Shanghai"}')
        self.assertEqual(events[4][1]["arguments"], '{"city":"Shanghai"}')
        self.assertEqual(events[-1][1]["response"]["output"][0]["name"], "get_weather")


if __name__ == "__main__":
    unittest.main()
