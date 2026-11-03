import asyncio, os, sys, time, json, wave, tempfile
OUT = os.environ.get('SINK_OUT') or tempfile.mkdtemp(prefix='sink-'); print('sink output dir', OUT, flush=True)
import websockets
chunks = []   # (t, nbytes)
pcm = bytearray()
async def h(ws):
    chunks.clear(); pcm.clear(); t0 = time.time(); print("sink: connected", flush=True)
    async for m in ws:
        chunks.append((time.time(), len(m))); pcm.extend(m)
    print("sink: closed after", round(time.time()-t0,1), flush=True)
    json.dump(chunks, open(os.path.join(OUT,"chunks.json"),"w"))
    with wave.open(os.path.join(OUT,"capture.wav"),"wb") as w: w.setnchannels(2); w.setsampwidth(2); w.setframerate(48000); w.writeframes(bytes(pcm))
async def main():
    async with websockets.serve(h, "127.0.0.1", 8799, max_size=None): await asyncio.Future()
asyncio.run(main())
