# GM Workstation

A lightweight, local-first Virtual Tabletop (VTT) and campaign management environment engineered for zero cloud dependencies, instant tactile response, and local AI co-GM assistance.

## Key Features

- **Spatial Subnet Hierarchy:** Models world, regional, settlement, and tactical maps using a CIDR network addressing topology (10.0.0.0/8 down to tactical rooms) with dynamic breadcrumb navigation.
- **Local AI Blueprint Engine:** Offline semantic parsing via Qwen 2.5 3B with GBNF grammars. Proposes atomic, reversible JSON blueprints before committing changes to the board.
- **FastAPI + SQLite WAL Backend:** High-concurrency, low-latency state persistence with zero server lockups.
- **Custom Map Underlay & Grid Snapping:** Drag-and-drop map uploads with real-time grid scaling, opacity, and alignment.
- **Real-Time Player Broadcast (/play):** WebSocket-driven player client running on local Wi-Fi. Strips GM controls and secrets while enabling real-time player token movement.

## Running the Server

    cd ~/gm-workstation
    source .venv/bin/activate
    python3 app.py

- **GM Screen:** http://127.0.0.1:8000/
- **Player Broadcast:** http://<LAN_IP>:8000/play
