"""FastAPI dashboard API and control endpoints."""

import os
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app.dashboard.state import dashboard_state
from app.risk import RiskManager


class ModeChangeRequest(BaseModel):
    mode: str
    confirmation: str | None = None


def create_dashboard_app(risk_manager: RiskManager) -> FastAPI:
    app = FastAPI(title="BTC 15M Arbitrage Dashboard", version="1.0.0")
    frontend_path = Path(__file__).parent / "frontend" / "index.html"

    @app.get("/", response_class=HTMLResponse)
    async def get_index():
        if not frontend_path.exists():
            raise HTTPException(status_code=404, detail="Dashboard UI file missing")
        return HTMLResponse(content=frontend_path.read_text(encoding="utf-8"))

    @app.get("/api/state")
    async def get_state():
        return dashboard_state.to_dict()

    @app.post("/api/controls/kill")
    async def trigger_kill():
        risk_manager.activate_kill_switch("Manual user trigger from dashboard")
        dashboard_state.kill_switch_active = True
        dashboard_state.kill_switch_reason = "Manual user trigger from dashboard"
        return {"status": "KILL_SWITCH_ACTIVATED"}

    @app.post("/api/controls/unkill")
    async def reset_kill():
        risk_manager.deactivate_kill_switch()
        dashboard_state.kill_switch_active = False
        dashboard_state.kill_switch_reason = None
        return {"status": "KILL_SWITCH_RESET"}

    @app.post("/api/controls/mode")
    async def change_mode(req: ModeChangeRequest):
        target = req.mode.upper()
        if target == "LIVE":
            if req.confirmation != "I understand this will submit real orders.":
                raise HTTPException(status_code=400, detail="Invalid live trading confirmation")
            success = risk_manager.enable_live_trading()
            if not success:
                raise HTTPException(status_code=400, detail="Cannot enable LIVE: credentials or balances not verified")
            dashboard_state.trading_mode = "LIVE"
            return {"status": "LIVE_ENABLED"}
        elif target in ("PAPER", "DATA"):
            risk_manager.disable_live_trading()
            dashboard_state.trading_mode = target
            return {"status": f"{target}_ENABLED"}
        raise HTTPException(status_code=400, detail=f"Unknown mode: {req.mode}")

    return app
