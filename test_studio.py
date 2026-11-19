import base64
import io
import json
import subprocess
import tempfile
import time
import wave
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from experiments import questions_for, variants_for
from detector import DetectorUnavailable, INFERENCE_LOCK, choose_detection, locate_subjects
from main import StudioHandler
from narration import NarrationError, collect_audio, narration_events
from soundtrack import LOOP_SECONDS, SoundtrackError, make_loop, music_prompt, run_musicgen_worker
from providers import StudioError, execute

OPENAI = {"provider": "openai", "model": "gpt-6.1-terra", "bridgeImages": True}
OLLAMA = {"provider": "ollama", "model": "sample-vision", "endpoint": "http://127.0.0.1:11434"}
OBSERVATION = {"setting": "Garden", "action": "A rabbit waits", "subjects": [{
    "label": "Rabbit", "detail": "Brown ears", "box": {"left": 120, "top": 180, "right": 570, "bottom": 750},
}]}
IMAGE = "data:image/jpeg;base64,YQ=="


class ProviderTests(unittest.TestCase):
    def test_openai_image_schema_and_cost(self):
        def request(url, payload, headers):
            self.assertEqual(url, "https://api.openai.com/v1/responses")
            self.assertEqual(headers["Authorization"], "Bearer fixture")
            self.assertFalse(payload["store"])
            self.assertTrue(payload["text"]["format"]["strict"])
            self.assertEqual(payload["temperature"], 0 if experiment else 1)
            self.assertNotIn("seed", payload)
            self.assertEqual(payload["input"][0]["content"][1]["image_url"], IMAGE)
            return {
                "model": "gpt-6.1-terra",
                "output": [{"content": [{"type": "output_text", "text": json.dumps(OBSERVATION)}]}],
                "usage": {"input_tokens": 100, "input_tokens_details": {"cached_tokens": 50}, "output_tokens": 20},
            }

        for experiment in (False, True):
            with self.subTest(experiment=experiment):
                result = execute({"action": "analyze", "settings": OPENAI, "data": {"image": IMAGE}},
                                 key="fixture", request=request, experiment=experiment)
        self.assertEqual(result["result"], OBSERVATION)
        self.assertAlmostEqual(result["metric"]["usd"], 0.0001525)

    def test_ollama_vision_and_usage(self):
        calls = []

        def request(url, payload, headers):
            calls.append(url)
            self.assertNotIn("Authorization", headers)
            if url.endswith("/api/show"):
                return {"capabilities": ["completion", "vision"]}
            self.assertEqual(payload["messages"][1]["images"], ["YQ=="])
            self.assertEqual(payload["format"]["type"], "object")
            self.assertEqual(payload["options"]["temperature"], 0)
            self.assertEqual(payload["options"]["seed"], 0)
            return {"message": {"content": json.dumps(OBSERVATION)}, "prompt_eval_count": 121, "eval_count": 31}

        result = execute({"action": "analyze", "settings": OLLAMA, "data": {"image": IMAGE}},
                         key="fixture", request=request, experiment=True)
        self.assertEqual(calls, ["http://127.0.0.1:11434/api/show", "http://127.0.0.1:11434/api/chat"])
        self.assertEqual(result["result"], OBSERVATION)
        self.assertEqual(result["metric"]["usage"], {"input_tokens": 121, "output_tokens": 31})
        self.assertIsNone(result["metric"]["usd"])

    def test_regular_ollama_requests_draw_a_fresh_seed(self):
        body = {"action": "check", "settings": OLLAMA}
        options = []

        def request(url, payload, _headers):
            if url.endswith("/api/show"):
                return {"capabilities": ["vision"]}
            options.append(payload["options"])
            return {"message": {"content": json.dumps({"result": "Fixture"})}}

        with patch("providers.randbelow", side_effect=[123, 456]):
            execute(body, request=request)
            execute(body, request=request)

        self.assertEqual([option["seed"] for option in options], [123, 456])
        self.assertEqual([option["temperature"] for option in options], [1, 1])

    def test_ollama_rejects_text_only_and_remote_hosts(self):
        def request(url, _payload, _headers):
            self.assertTrue(url.endswith("/api/show"))
            return {"capabilities": ["completion"]}

        with self.assertRaisesRegex(StudioError, "no vision capability"):
            execute({"action": "check", "settings": OLLAMA}, request=request)
        with self.assertRaisesRegex(StudioError, "loopback"):
            execute({"action": "models", "settings": {**OLLAMA, "endpoint": "https://example.com"}})

    def test_illustration_has_separate_metric(self):
        def request(url, payload, _headers):
            self.assertTrue(url.endswith("/images/generations"))
            self.assertEqual(payload["model"], "gpt-image-2.6-flare")
            self.assertIn("rabbit finds the path", payload["prompt"])
            return {"data": [{"b64_json": "YQ=="}], "usage": {
                "input_tokens_details": {"text_tokens": 50, "image_tokens": 0}, "output_tokens": 100,
            }}

        result = execute({"action": "image", "settings": OPENAI, "data": {"event": "A rabbit finds the path."}},
                         key="fixture", request=request)
        self.assertEqual(result["result"], "data:image/png;base64,YQ==")
        self.assertEqual(result["metric"]["action"], "image")
        self.assertAlmostEqual(result["metric"]["usd"], 0.00325)

    def test_invalid_provider_output_is_retryable(self):
        def request(_url, _payload, _headers):
            return {"message": {"content": '{"title":"Garden","scenes":"invalid"}'}}

        with self.assertRaisesRegex(StudioError, "unexpected result"):
            execute({"action": "story", "settings": OLLAMA, "data": {"scenes": []}}, request=request)


class DetectorTests(unittest.TestCase):
    def test_confidence_and_instance_ambiguity(self):
        left = (10, 10, 40, 40)
        right = (60, 10, 90, 40)
        duplicates = [(left, 0.51), ((11, 11, 41, 41), 0.50)]
        self.assertEqual(choose_detection(duplicates, None), (left, 0.51))
        self.assertIsNone(choose_detection([(left, 0.2)], None))
        self.assertIsNone(choose_detection([(left, 0.52), (right, 0.48)], None))
        self.assertEqual(choose_detection([(left, 0.52), (right, 0.48)], right), (right, 0.48))

    def test_detector_skips_busy_work_and_releases_lock_after_a_timeout(self):
        with INFERENCE_LOCK:
            with self.assertRaisesRegex(DetectorUnavailable, "busy"):
                locate_subjects(IMAGE, [{"label": "rabbit"}])
        with patch("detector.DETECTION_SECONDS", 0), patch("detector._load_detector"):
            with self.assertRaisesRegex(DetectorUnavailable, "took too long"):
                locate_subjects(IMAGE, [{"label": "rabbit"}])
        self.assertFalse(INFERENCE_LOCK.locked())

    def test_local_detection_needs_no_provider_credentials(self):
        payload = {"action": "localize", "data": {"image": IMAGE, "candidates": [{"label": "rabbit"}]}}
        with patch("providers.locate_subjects", return_value=[None]):
            answer = execute(payload)
            self.assertEqual(answer["result"], {"available": True, "detections": [None]})
            self.assertEqual(answer["metric"]["action"], "localize")
        with patch("providers.locate_subjects", side_effect=DetectorUnavailable("Weights missing.")):
            self.assertFalse(execute(payload)["result"]["available"])


class NarrationTests(unittest.TestCase):
    def test_realtime_pcm_becomes_a_playable_wav(self):
        pcm = b"\x01\x00\x02\x00"
        connection = unittest.mock.Mock()
        connection.recv.side_effect = [json.dumps(event) for event in (
            {"type": "session.updated"},
            {"type": "response.output_audio.delta", "delta": base64.b64encode(pcm).decode()},
            {"type": "response.done", "response": {"status": "completed"}},
        )]
        audio = collect_audio(connection, narration_events("Pip found the trail.", "Mystery"))
        with wave.open(io.BytesIO(audio)) as recording:
            self.assertEqual((recording.getnchannels(), recording.getsampwidth(), recording.getframerate()), (1, 2, 24000))
            self.assertEqual(recording.readframes(2), pcm)
        response = json.loads(connection.send.call_args_list[1].args[0])["response"]
        self.assertEqual(response["input"][0]["content"][0]["text"], "Pip found the trail.")
        self.assertIn("gentle suspense", response["instructions"])

    def test_incomplete_audio_reports_an_error(self):
        connection = unittest.mock.Mock()
        connection.recv.return_value = '{"type":"response.done","response":{"status":"incomplete"}}'
        with self.assertRaisesRegex(NarrationError, "did not finish"):
            collect_audio(connection, narration_events("Pip found the trail.", ""))


class SoundtrackTests(unittest.TestCase):
    def test_prompt_uses_the_theme_and_tone(self):
        prompt = music_prompt({"description": "Soft guitar for a woodland trail.", "tone": "Mystery"})
        self.assertIn("woodland trail", prompt)
        self.assertIn("mysterious", prompt)
        self.assertIn("without vocals", prompt)

    def test_musicgen_worker_has_a_hard_timeout(self):
        with patch("soundtrack.subprocess.run", side_effect=subprocess.TimeoutExpired("MusicGen", 300)) as worker:
            with self.assertRaisesRegex(SoundtrackError, "five-minute server limit"):
                run_musicgen_worker({"description": "Soft piano"})
        self.assertEqual(worker.call_args.kwargs["timeout"], 300)

    def test_wav_loop_rejects_a_short_burst_with_a_long_quiet_tail(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest("The optional MusicGen dependencies are unavailable.")
        rate = 1000
        samples = np.full((1, 21 * rate), 0.0001, dtype=np.float32)
        samples[:, :rate * 3] = 0.15
        with self.assertRaisesRegex(SoundtrackError, "mostly quiet"):
            make_loop(samples, rate)

    def test_wav_loop_has_the_requested_duration_and_a_continuous_seam(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest("The optional MusicGen dependencies are unavailable.")
        rate = 1000
        values = np.linspace(0.1, 0.6, 21 * rate, dtype=np.float32)
        with wave.open(io.BytesIO(make_loop(values[None, :], rate))) as recording:
            self.assertEqual(recording.getnframes(), LOOP_SECONDS * rate)
            samples = np.frombuffer(recording.readframes(recording.getnframes()), dtype="<i2")
        self.assertLessEqual(abs(int(samples[0]) - int(samples[-1])), 2)


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), StudioHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def request(self, method, path, body=None, headers=None):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request(method, path, body, headers or {})
            response = connection.getresponse()
            return response.status, response.headers, response.read()
        finally:
            connection.close()

    def test_serves_the_existing_interface(self):
        for path, mime in (("/", "text/html"), ("/studio.js", "text/javascript"), ("/studio.css", "text/css")):
            status, headers, content = self.request("GET", path)
            self.assertEqual(status, 200)
            self.assertIn(mime, headers["Content-Type"])
            self.assertIn("frame-ancestors", headers["Content-Security-Policy"])
            self.assertTrue(content)

    def test_audio_route_returns_wave_for_openai_only(self):
        payload = {"action": "narrate", "settings": OPENAI, "data": {"text": "A story."}}
        headers = {"Content-Type": "application/json", "X-OpenAI-Key": "fixture"}
        with patch("main.synthesize", return_value=b"RIFF-fixture") as synthesize:
            status, response_headers, body = self.request("POST", "/api/run", json.dumps(payload), headers)
            self.assertEqual(status, 200)
            self.assertEqual(response_headers["Content-Type"], "audio/wav")
            self.assertIn("media-src 'self' blob:", response_headers["Content-Security-Policy"])
            self.assertEqual(body, b"RIFF-fixture")
            synthesize.assert_called_once_with(payload["data"], key="fixture")
            payload["settings"] = OLLAMA
            self.assertEqual(self.request("POST", "/api/run", json.dumps(payload), headers)[0], 400)
            self.assertEqual(synthesize.call_count, 1)

    def test_soundtrack_starts_and_polls_without_holding_the_request_open(self):
        ready = threading.Event()
        headers = {"Content-Type": "application/json"}
        description = {"description": "Soft piano", "tone": "Mystery"}

        def send(action, data):
            return self.request("POST", "/api/run", json.dumps({"action": action, "data": data}), headers)

        def render(data):
            self.assertEqual(data, description)
            self.assertTrue(ready.wait(5))
            return b"RIFF-loop"

        with patch("soundtrack.run_musicgen_worker", side_effect=render):
            try:
                status, _, body = send("soundtrack-start", description)
                self.assertEqual(status, 202)
                job = {"job": json.loads(body)["result"]["job"]}
                status, _, body = send("soundtrack-result", job)
                self.assertEqual(status, 202)
                self.assertEqual(json.loads(body)["result"]["status"], "composing")
            finally:
                ready.set()
            for _ in range(20):
                status, response_headers, body = send("soundtrack-result", job)
                if status != 202:
                    break
            self.assertEqual((status, response_headers["Content-Type"], body), (200, "audio/wav", b"RIFF-loop"))

    def test_experiment_batch_saves_media_and_validates_votes(self):
        image = {"id": "image-1", "name": "Moon", "src": IMAGE}
        data = {"tones": ["Humorous", "Mystery"], "levels": ["Beginning"], "lengths": [250],
                "settings": OPENAI, "images": [image, {**image, "id": "image-2"}],
                "observations": [{"scene": 1, "action": "Pip looks up", "setting": "Garden", "subjects": []},
                                 {"scene": 2, "action": "Pip heads home", "setting": "Garden", "subjects": []}],
                "subjects": []}
        self.assertEqual(len(variants_for(data)), 2)

        def model(body, **_kwargs):
            results = {
                "plan": {"bridges": [{"after": 1, "description": "Pip follows the moonlight."}]},
                "image": IMAGE.replace("image/jpeg", "image/png"),
                "story": {"title": "The Moon", "musicDescription": "Piano",
                          "scenes": [{"scene": index, "text": "Pip saw the moon."} for index in range(1, 4)]},
            }
            return {"result": results[body["action"]],
                    "metric": {"model": "fixture", "usage": {"input_tokens": 100, "output_tokens": 20}}}

        host = "framefable.example.com"
        origin = f"https://{host}"
        headers = {"Host": host}

        def post(action, **fields):
            request_headers = {**headers, "Origin": origin, "Content-Type": "application/json"}
            if action == "create":
                request_headers["X-OpenAI-Key"] = "fixture"
            status, _, response = self.request("POST", "/api/experiments", json.dumps({"action": action, **fields}),
                                               request_headers)
            return status, json.loads(response)

        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"FRAMEFABLE_EXPERIMENTS_DIR": directory,
                                                                               "FRAMEFABLE_PUBLIC_ORIGIN": origin}):
            with patch("experiments.execute", side_effect=model), patch("experiments.make_story_pdf", return_value=b"%PDF-fixture"), patch("experiments.run_musicgen_worker", return_value=b"RIFF-fixture"), patch("experiments.synthesize", return_value=b"RIFF-voice"):
                status, created = post("create", data=data)
                self.assertEqual(status, 200, created)
                ident = created["id"]
                for _ in range(100):
                    _, progress = post("status", id=ident)
                    if progress["status"] != "running":
                        break
                    time.sleep(0.01)
                self.assertEqual(progress["status"], "completed")
            _, review = post("review", id=ident)
            self.assertEqual([item["wordCount"] for item in review["variants"]], [12, 12])
            self.assertIn("bridge-1.png", review["variants"][0]["media"])
            root = f"/api/experiments/{ident}/1"
            status, image_headers, image_data = self.request("GET", root + "/bridge-1.png", headers=headers)
            self.assertEqual((status, image_headers["Content-Type"], image_data), (200, "image/png", b"a"))
            status, pdf_headers, pdf_data = self.request("GET", root + "/story.pdf", headers=headers)
            self.assertEqual((status, pdf_data), (200, b"%PDF-fixture"))
            self.assertIn("frame-ancestors 'self'", pdf_headers["Content-Security-Policy"])
            status, _, audio = self.request("GET", root + "/music.wav", headers={**headers, "Range": "bytes=0-3"})
            self.assertEqual((status, audio), (206, b"RIFF"))
            self.assertEqual(post("vote", id=ident, voter="Reviewer", ratings={})[0], 400)
            votes = {str(item["index"]): {key: 4 for key in questions_for(item)} for item in review["variants"]}
            status, report = post("vote", id=ident, voter="Reviewer", ratings=votes)
            self.assertEqual((status, report["meanScore"]), (200, 4))
            pdf = base64.b64decode(report.pop("reportPdf"), validate=True)
            self.assertTrue(pdf.startswith(b"%PDF-"))
            self.assertEqual(report["ratings"], votes)
            for criterion in ("subject_accuracy", "box_accuracy"):
                self.assertEqual(report["criteria"][criterion]["distribution"], [0, 0, 0, 2, 0])

    def test_hosted_requests_require_the_exact_https_origin(self):
        hostname = "stories.example.com"
        public = f"https://{hostname}"
        headers = {"Host": hostname, "Content-Type": "application/json"}
        with patch.dict("os.environ", {"FRAMEFABLE_PUBLIC_ORIGIN": public}):
            self.assertEqual(self.request("GET", "/", headers=headers)[0], 200)
            with patch("main.execute", return_value={"result": "ok"}) as run:
                for origin in ("", "http://stories.example.com", "https://other.example.com"):
                    self.assertEqual(self.request("POST", "/api/run", "{}", {**headers, "Origin": origin})[0], 403)
                self.assertEqual(self.request("POST", "/api/run", "{}", {**headers, "Origin": public})[0], 200)
                run.assert_called_once()
            self.assertEqual(self.request("GET", "/", headers={"Host": "other.example.com"})[0], 403)

    def test_api_routes_requests_and_rejects_foreign_origins(self):
        body = {"action": "check", "settings": OPENAI}
        answer = {"result": {"result": "Fixture"}, "metric": {"action": "check"}}
        headers = {"Content-Type": "application/json", "X-OpenAI-Key": "fixture"}
        with patch("main.execute", return_value=answer) as run:
            status, _, content = self.request("POST", "/api/run", json.dumps(body), headers)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(content), answer)
            run.assert_called_once_with(body, key="fixture")

            status, _, _ = self.request("POST", "/api/run", "{}", {**headers, "Origin": "https://example.com"})
            self.assertEqual(status, 403)
            self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
