"""Run the dashboard: `uv run python -m app` or `uv run uvicorn app.main:app`."""
import uvicorn

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="127.0.0.1", port=8077, reload=True)