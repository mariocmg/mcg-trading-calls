import net from 'net';
import tls from 'tls';
import crypto from 'crypto';

// Transporte websocket manual: tunel CONNECT + TLS + upgrade HTTP tolerante
// (el proxy del sandbox devuelve 101 con "connection: close" y el cliente
// `ws` lo rechaza; aqui se acepta el 101 si el accept coincide).
export function createRawTransport(proxyUrl) {
  const pu = new URL(proxyUrl);
  const proxyAuth = pu.username ? `Proxy-Authorization: Basic ${Buffer.from(`${decodeURIComponent(pu.username)}:${decodeURIComponent(pu.password)}`).toString('base64')}\r\n` : '';
  return (request, handlers) => {
    const u = new URL(request.url);
    const host = u.hostname, port = parseInt(u.port || '443', 10);
    let socket = null, buf = Buffer.alloc(0), upgraded = false, closed = false;
    let frag = null; // fragmentos en curso
    const key = crypto.randomBytes(16).toString('base64');
    const accept = crypto.createHash('sha1').update(key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest('base64');

    function fail(e) { if (!closed) { closed = true; try { socket?.destroy(); } catch {} handlers.onError(e); } }
    function parseFrames() {
      for (;;) {
        if (buf.length < 2) return;
        const b0 = buf[0], b1 = buf[1];
        const fin = b0 & 0x80, op = b0 & 0x0f;
        let len = b1 & 0x7f, off = 2;
        if (len === 126) { if (buf.length < 4) return; len = buf.readUInt16BE(2); off = 4; }
        else if (len === 127) { if (buf.length < 10) return; len = Number(buf.readBigUInt64BE(2)); off = 10; }
        if (buf.length < off + len) return;
        const payload = buf.subarray(off, off + len);
        buf = buf.subarray(off + len);
        if (op === 0x9) { sendFrame(0xA, payload); continue; }           // ping -> pong
        if (op === 0xA || op === 0x8) { if (op === 0x8) closeSocket(); continue; }
        if (op === 0x0) { if (frag !== null) { frag = Buffer.concat([frag, payload]); if (fin) { const m = frag; frag = null; handlers.onMessage(m.toString('utf8')); } } continue; }
        if (op === 0x1 || op === 0x2) {
          if (fin) handlers.onMessage(payload.toString('utf8'));
          else frag = Buffer.from(payload);
        }
      }
    }
    function sendFrame(op, payload) {
      if (!socket || socket.destroyed) return;
      const mask = crypto.randomBytes(4);
      const masked = Buffer.alloc(payload.length);
      for (let i = 0; i < payload.length; i++) masked[i] = payload[i] ^ mask[i % 4];
      let head;
      if (payload.length < 126) { head = Buffer.from([0x80 | op, 0x80 | payload.length]); }
      else if (payload.length < 65536) { head = Buffer.alloc(4); head[0] = 0x80 | op; head[1] = 0x80 | 126; head.writeUInt16BE(payload.length, 2); }
      else { head = Buffer.alloc(10); head[0] = 0x80 | op; head[1] = 0x80 | 127; head.writeBigUInt64BE(BigInt(payload.length), 2); }
      socket.write(Buffer.concat([head, mask, masked]));
    }
    function closeSocket() {
      if (closed) return; closed = true;
      try { socket?.end(); } catch {}
      handlers.onClose(1000, '');
    }
    function onData(chunk) {
      buf = Buffer.concat([buf, chunk]);
      if (!upgraded) {
        const idx = buf.indexOf('\r\n\r\n');
        if (idx === -1) { if (buf.length > 65536) fail(new Error('headers demasiado largos')); return; }
        const head = buf.subarray(0, idx).toString('latin1');
        buf = buf.subarray(idx + 4);
        const m = head.match(/^HTTP\/\d\.\d (\d+)/);
        const acc = (head.match(/sec-websocket-accept:\s*(\S+)/i) || [])[1];
        if (!m || m[1] !== '101' || acc !== accept) { fail(new Error(`upgrade rechazado: ${head.split('\r\n')[0]} accept=${acc}`)); return; }
        upgraded = true;
        handlers.onOpen();
      }
      parseFrames();
    }
    const tcp = net.connect({ host: pu.hostname, port: parseInt(pu.port || '3128', 10) }, () => {
      tcp.write(`CONNECT ${host}:${port} HTTP/1.1\r\nHost: ${host}:${port}\r\n${proxyAuth}\r\n`);
    });
    let connectBuf = Buffer.alloc(0);
    tcp.on('data', (chunk) => {
      if (socket) return; // ya en TLS
      connectBuf = Buffer.concat([connectBuf, chunk]);
      const idx = connectBuf.indexOf('\r\n\r\n');
      if (idx === -1) return;
      const head = connectBuf.subarray(0, idx).toString('latin1');
      if (!/^HTTP\/\d\.\d 2\d\d/.test(head)) { fail(new Error(`CONNECT rechazado: ${head.split('\r\n')[0]}`)); return; }
      const rest = connectBuf.subarray(idx + 4);
      socket = tls.connect({ socket: tcp, servername: host, rejectUnauthorized: false }, () => {
        const headers = { ...request.headers, 'Sec-WebSocket-Key': key, 'Sec-WebSocket-Version': '13', Connection: 'Upgrade', Upgrade: 'websocket' };
        let req = `GET ${u.pathname}${u.search} HTTP/1.1\r\nHost: ${host}\r\nOrigin: ${request.origin}\r\n`;
        for (const [k, v] of Object.entries(headers)) req += `${k}: ${v}\r\n`;
        socket.write(req + '\r\n');
        if (rest.length) onData(rest);
      });
      socket.on('data', onData);
      socket.on('close', () => { if (!closed) { closed = true; handlers.onClose(1006, ''); } });
      socket.on('error', fail);
    });
    tcp.on('error', fail);
    return {
      get isOpen() { return !!socket && !socket.destroyed && upgraded; },
      send(data) { sendFrame(0x1, Buffer.from(typeof data === 'string' ? data : String(data), 'utf8')); },
      close() { closeSocket(); },
      terminate() { if (!closed) { closed = true; try { socket?.destroy(); tcp.destroy(); } catch {} } },
    };
  };
}
