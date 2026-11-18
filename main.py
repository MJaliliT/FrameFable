"""Serve FrameFable on this computer."""

import errno
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from narration import NarrationError, synthesize
from providers import StudioError, execute
from soundtrack import SoundtrackError, soundtrack_result, start_soundtrack
from story_pdf import StoryPdfError, make_story_pdf

PUBLIC = Path(__file__).resolve().parent / "public"
STATIC_FILES = {
    "/": ("index.html", "text/html"),
    "/studio.js": ("studio.js", "text/javascript"),
    "/studio.css": ("studio.css", "text/css"),
}
MAX_BODY = 3_000_000
MAX_PDF_BODY = 40_000_000
CSP = "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; base-uri 'none'; frame-ancestors 'none'"


def configured_public_origin():
    origin = os.environ.get("FRAMEFABLE_PUBLIC_ORIGIN", "")
    if not origin:
        return ""
    try:
        url = urlsplit(origin)
    except ValueError:
        return ""
    if url.scheme == "https" and url.hostname and "." in url.hostname and origin == f"https://{url.hostname}":
        if re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", url.hostname):
            return origin
    return ""


class StudioHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        # Avoid writing details from local requests to logs.
        pass

    def send(self, status, body=b"", content_type=None):
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Cache-Control", "no-store")
        if content_type:
            self.send_header("Content-Type", content_type)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_json(self, status, value):
        self.send(status, json.dumps(value).encode(), "application/json; charset=utf-8")

    def request_origin(self):
        host = self.headers.get("Host", "")
        if re.fullmatch(r"(127\.0\.0\.1|localhost)(:\d+)?", host):
            return "http://" + host
        origin = configured_public_origin()
        return origin if origin and origin == "https://" + host else ""

    def do_GET(self):
        if not self.request_origin():
            self.send(403)
            return
        static = STATIC_FILES.get(self.path)
        if not static:
            self.send(404)
            return
        name, content_type = static
        try:
            contents = (PUBLIC / name).read_bytes()
        except OSError:
            self.send(500)
            return
        self.send(200, contents, content_type + "; charset=utf-8")

    def do_POST(self):
        expected_origin = self.request_origin()
        if not expected_origin:
            self.send(403)
            return
        if self.path not in ("/api/run", "/api/story.pdf"):
            self.send(404)
            return
        origin = self.headers.get("Origin")
        origin_required = bool(origin) or expected_origin.startswith("https:")
        if ((origin_required and origin != expected_origin)
                or self.headers.get("Content-Type") != "application/json"):
            self.send(403)
            return
        try:
            size = int(self.headers.get("Content-Length", ""))
            if size < 0:
                raise ValueError
        except ValueError:
            self.send_json(400, {"error": "The request needs a valid content length."})
            return
        limit = MAX_PDF_BODY if self.path == "/api/story.pdf" else MAX_BODY
        if size > limit:
            self.send_json(413, {"error": "The request is too large."})
            return
        try:
            body = json.loads(self.rfile.read(size))
        except (ValueError, UnicodeError, RecursionError):
            self.send_json(400, {"error": "The request must contain valid JSON."})
            return
        if self.path == "/api/story.pdf":
            try:
                pdf = make_story_pdf(body)
            except StoryPdfError as error:
                self.send_json(400, {"error": str(error)})
                return
            self.send(200, pdf, "application/pdf")
            return
        key = self.headers.get("X-OpenAI-Key")
        if isinstance(body, dict) and body.get("action") == "narrate":
            settings = body.get("settings")
            if not isinstance(settings, dict) or settings.get("provider") != "openai":
                self.send_json(400, {"error": "Narration is available for OpenAI stories."})
                return
            try:
                audio = synthesize(body.get("data"), key=key)
            except NarrationError as error:
                self.send_json(400, {"error": str(error)})
                return
            self.send(200, audio, "audio/wav")
            return
        action = body.get("action") if isinstance(body, dict) else None
        if action in ("soundtrack-start", "soundtrack-result"):
            try:
                if action == "soundtrack-start":
                    job = start_soundtrack(body.get("data"))
                    self.send_json(202, {"result": {"job": job}})
                else:
                    audio = soundtrack_result(body.get("data"))
                    if audio is None:
                        self.send_json(202, {"result": {"status": "composing"}})
                    else:
                        self.send(200, audio, "audio/wav")
            except SoundtrackError as error:
                self.send_json(400, {"error": str(error)})
            return
        try:
            result = execute(body, key=key)
        except StudioError as error:
            self.send_json(400, {"error": str(error)})
            return
        except (TypeError, AttributeError, RecursionError):
            self.send_json(502, {"error": "The provider returned an unexpected response."})
            return
        self.send_json(200, result)


def main():
    public_origin = configured_public_origin()
    if os.environ.get("FRAMEFABLE_PUBLIC_ORIGIN") and not public_origin:
        raise SystemExit("FRAMEFABLE_PUBLIC_ORIGIN must be an HTTPS origin such as https://stories.example.com.")
    try:
        port = int(os.environ.get("PORT", "4173"))
        if not 0 <= port <= 65535:
            raise ValueError
    except ValueError:
        raise SystemExit("PORT must be an integer from 0 to 65535.") from None

    host = "127.0.0.1"
    if public_origin and not port:
        raise SystemExit("Choose a fixed PORT when using FRAMEFABLE_PUBLIC_ORIGIN.")
    try:
        server = ThreadingHTTPServer((host, port), StudioHandler)
    except OSError as error:
        if error.errno != errno.EADDRINUSE or port == 0 or public_origin:
            raise
        server = ThreadingHTTPServer((host, 0), StudioHandler)
        print(f"Port {port} is in use. Using port {server.server_port} instead.", flush=True)


    with server:
        print(f"FrameFable is running at http://127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
