"""Test doubles for the two systems KukHire talks to. They exist ONLY for tests.

* FakeAuthServer implements the AuthKit REST endpoints KukHire consumes (login, otp/verify,
  token/refresh, logout, me) as a real HTTP server, so KukHire's real verifier is exercised over
  the network.
* FakeHrmsServer implements the CONTRACT of POST /api/v1/hr/hires (idempotent on the reference).
  The real endpoint is verified against a running KukBook in tests/e2e/test_real_hrms.py.
"""
from __future__ import annotations

import json
import secrets
import socket
import threading
import time

import uvicorn
from fastapi import FastAPI, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LiveServer:
    def __init__(self, app: FastAPI) -> None:
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self._server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning"))
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def __enter__(self) -> "LiveServer":
        self._thread.start()
        deadline = time.time() + 10
        while not self._server.started:
            if time.time() > deadline:
                raise RuntimeError("server did not start")
            time.sleep(0.02)
        return self

    def __exit__(self, *exc) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10)


class FakeAuthServer:
    """users: {identifier: {"password": str, "id": str, "name": str, "email": str, "otp": str|None}}"""

    def __init__(self) -> None:
        self.users: dict[str, dict] = {}
        self.access: dict[str, dict] = {}
        self.refresh: dict[str, dict] = {}
        self.me_calls = 0
        self.app = self._build()

    def add_user(self, identifier: str, *, password: str = "pw", name: str | None = None, otp: str | None = None, verified: bool = True) -> None:
        self.users[identifier] = {"password": password, "id": f"kuk-{identifier.split('@')[0]}", "name": name or identifier.split("@")[0].title(),
                                  "email": identifier, "otp": otp, "verified": verified}

    def _issue(self, user: dict) -> dict:
        access, refresh = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        self.access[access] = user
        self.refresh[refresh] = user
        return {"access_token": access, "refresh_token": refresh, "user": self._payload(user)}

    @staticmethod
    def _payload(u: dict) -> dict:
        return {"kuklabs_user_id": u["id"], "id": 1, "full_name": u["name"], "email": u["email"], "phone": None,
                "email_verified": u["verified"], "phone_verified": False}

    def expire_access_tokens(self) -> None:
        self.access.clear()

    def _build(self) -> FastAPI:
        app = FastAPI()
        app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

        @app.post("/v1/auth/login")
        async def login(request: Request):
            body = await request.json()
            u = self.users.get(body.get("identifier"))
            if not u or u["password"] != body.get("password"):
                return JSONResponse({"error": True, "message": "Invalid credentials."}, status_code=401)
            if u["otp"]:
                return JSONResponse({"error": True, "status": "otp_required", "message": "Enter the code."}, status_code=403)
            return self._issue(u)

        @app.post("/v1/auth/otp/verify")
        async def otp(request: Request):
            body = await request.json()
            u = self.users.get(body.get("identifier"))
            if not u or u["otp"] != body.get("code"):
                return JSONResponse({"error": True, "message": "Invalid code."}, status_code=400)
            return self._issue(u)

        @app.post("/v1/auth/token/refresh")
        async def refresh(request: Request):
            body = await request.json()
            u = self.refresh.pop(body.get("refresh_token"), None)
            if not u:
                return JSONResponse({"error": True, "message": "Invalid refresh token."}, status_code=401)
            return self._issue(u)

        @app.post("/v1/auth/logout")
        async def logout():
            return {"success": True}

        @app.get("/v1/auth/me")
        async def me(authorization: str = Header(default="")):
            self.me_calls += 1
            u = self.access.get(authorization.removeprefix("Bearer ").strip())
            if not u:
                return JSONResponse({"error": True, "message": "Session expired."}, status_code=401)
            return {"user": self._payload(u)}

        return app


class FakeHrmsServer:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self.by_ref: dict[str, dict] = {}
        self.requests: list[dict] = []
        self.app = self._build()

    def _build(self) -> FastAPI:
        app = FastAPI()

        @app.post("/api/v1/hr/hires")
        async def hires(request: Request, authorization: str = Header(default="")):
            if authorization != f"Bearer {self.api_key}":
                return JSONResponse({"error": "Invalid or revoked API key"}, status_code=401)
            body = await request.json()
            self.requests.append(body)
            ref = body["externalApplicationRef"]
            if ref in self.by_ref:
                return {"data": {**self.by_ref[ref], "created": False}}
            n = 500 + len(self.by_ref) + 1
            rec = {"handoffId": n, "employeeId": n, "empId": f"EMP-{n}", "onboardingChecklistId": None, "referralBonusStatus": "none"}
            self.by_ref[ref] = rec
            return JSONResponse({"data": {**rec, "created": True}}, status_code=201)

        return app
