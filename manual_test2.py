import asyncio, json, tempfile
from pathlib import Path
from runtime.session import EngineSession
from runtime.store.edits import ensure_schema
from runtime.http_server import HttpServer

async def run():
    tmp = Path(tempfile.mkdtemp(prefix="eh_"))
    db = tmp / "session.db"
    ensure_schema(db)
    sess = EngineSession(tmp, db)
    await sess.start()
    server = HttpServer(sess, "127.0.0.1", 0)
    serve_task = asyncio.create_task(server.serve())
    try:
        while server.bound_port is None:
            await asyncio.sleep(0.01)
        host, port = "127.0.0.1", server.bound_port
        print("connecting", flush=True)
        reader, writer = await asyncio.open_connection(host, port)
        writer.write(f"GET /events HTTP/1.1\r\nHost: {host}\r\n\r\n".encode())
        await writer.drain()
        print("wrote get", flush=True)
        status_line = await asyncio.wait_for(reader.readline(), timeout=2.0)
        print("status", status_line, flush=True)
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=2.0)
            print("hdr", line, flush=True)
            if line in (b"\r\n", b"\n", b""):
                break

        async def trigger():
            await asyncio.sleep(0.05)
            r2, w2 = await asyncio.open_connection(host, port)
            body = json.dumps({"type": "ListSessions"}).encode()
            req = f"POST /command HTTP/1.1\r\nHost: {host}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body
            w2.write(req)
            await w2.drain()
            resp = await r2.read()
            print("post resp", resp, flush=True)

        t = asyncio.create_task(trigger())
        print("awaiting data line", flush=True)
        data_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
        print("data line", data_line, flush=True)
        await t
        writer.close()
    finally:
        server.stop()
        await serve_task
    print("done", flush=True)

asyncio.run(run())
