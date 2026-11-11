"""HTTP endpoint for the WhatsApp Cloud API webhook.

The handler class is created per server and closes over that server's
callbacks. The usual web.py pattern resolves a module-path string to a
singleton channel, which ties the endpoint to one global instance. This layer
knows nothing about signatures or payloads. It hands the raw body and the
signature header to the channel and returns whatever status the channel decides.

Hardening:
* request bodies are capped (cheroot answers 413) before anything reads them;
* no ``StaticMiddleware``, so nothing from the working directory is served;
* any callback exception becomes a bare 500, never web.py's debug page.
"""

import web

from common.log import logger

WEBHOOK_PATH = "/whatsapp/webhook"
MAX_REQUEST_BODY = 4 * 1024 * 1024


def build_wsgi_app(on_verify, on_event) -> web.application:
    """``on_verify(mode, token, challenge) -> Optional[str]``; ``on_event(raw, signature) -> int``."""

    class WebhookHandler:
        def GET(self):
            try:
                params = web.input(**{"hub.mode": None, "hub.verify_token": None, "hub.challenge": None})
                challenge = on_verify(params["hub.mode"], params["hub.verify_token"], params["hub.challenge"])
            except Exception as e:
                logger.error(f"[WhatsApp] webhook verify handler error: {e}", exc_info=True)
                raise web.internalerror("internal error") from None
            if challenge is None:
                raise web.forbidden("forbidden")
            web.header("Content-Type", "text/plain")
            return challenge

        def POST(self):
            try:
                status = on_event(web.data(), web.ctx.env.get("HTTP_X_HUB_SIGNATURE_256"))
            except Exception as e:
                logger.error(f"[WhatsApp] webhook event handler error: {e}", exc_info=True)
                raise web.internalerror("internal error") from None
            if status == 403:
                raise web.forbidden("forbidden")
            if status != 200:
                raise web.internalerror("internal error")
            return "OK"

    app = web.application((WEBHOOK_PATH, WebhookHandler), {}, autoreload=False)
    # web.py renders a traceback page on unhandled errors when debug is on;
    # keep it off so internals never reach the caller.
    app.internalerror = lambda: web.internalerror("internal error")
    return app


class WebhookServer:
    """cheroot server for one channel. ``prepare()`` binds (and raises on failure); ``serve()`` blocks."""

    def __init__(self, host, port, on_verify, on_event, max_body=MAX_REQUEST_BODY):
        self._app = build_wsgi_app(on_verify, on_event)
        self._server = web.httpserver.WSGIServer((host, int(port)), self._app.wsgifunc())
        self._server.max_request_body_size = max_body

    @property
    def bound_port(self) -> int:
        return self._server.bind_addr[1]

    def prepare(self):
        self._server.prepare()

    def serve(self):
        self._server.serve()

    def stop(self):
        self._server.stop()
