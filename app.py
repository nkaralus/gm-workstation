"""Entry point:  python app.py   (or: gm-workstation)

Binds to 0.0.0.0 so phones, tablets and laptops on the same Wi-Fi can open the player view:
    GM (this computer only):  http://127.0.0.1:8000/
    Players:                  http://<this computer's LAN address>:8000/play
Set HOST=127.0.0.1 to keep everything on this machine. Other devices can only ever reach /play; the GM tools answer to
the host machine alone (GM_ALLOW_LAN=1 lets other devices act as GM, e.g. an iPad).
"""
import os

from engine import app, lan_ip  # noqa: F401  (app re-exported for `uvicorn app:app`)


def main() -> None:
    import uvicorn

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    os.environ["GM_BIND_HOST"] = host                    # lets the GM page say whether players can reach this server
    ip = lan_ip()
    print(f"\n  GM view      http://127.0.0.1:{port}/")
    if host in ("127.0.0.1", "localhost", "::1"):
        print("  Player link  (server is bound to this computer only; start with HOST=0.0.0.0 to share)")
    else:
        print(f"  Player link  http://{ip or '<this computer address>'}:{port}/play   <- share this on your Wi-Fi\n")
    uvicorn.run("engine:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
