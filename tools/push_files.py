import serial, time, sys, os, binascii, hashlib

ROOT = sys.argv[1]
FILES = sys.argv[2:]
s = serial.Serial('COM7', 115200, timeout=0.2)

def rd_until(term, t=30):
    b = b''
    end = time.time() + t
    while time.time() < end:
        b += s.read(65536)
        if term in b:
            return b
    return b

def exec_raw(code, t=30):
    s.write(code.encode() + b'\x04')
    b = rd_until(b'\x04>', t)
    b = b.split(b'OK', 1)[1] if b'OK' in b else b
    parts = b.split(b'\x04')
    return parts[0].decode(errors='replace'), (parts[1] if len(parts) > 1 else b'').decode(errors='replace')

s.write(b'\x03\x03'); time.sleep(0.5); s.read(65536)
s.write(b'\x01'); rd_until(b'raw REPL; CTRL-B to exit\r\n>', 5)

for rel in FILES:
    data = open(os.path.join(ROOT, rel), 'rb').read()
    dev = '/' + rel.replace('\\', '/')
    tmp = dev + '.tmp'
    out, err = exec_raw("import binascii\nf=open(%r,'wb')" % tmp)
    for i in range(0, len(data), 512):
        chunk = binascii.b2a_base64(data[i:i+512]).decode().strip()
        out, err = exec_raw("f.write(binascii.a2b_base64(%r))" % chunk)
        if err.strip():
            sys.exit('write error %s: %s' % (dev, err))
    out, err = exec_raw(
        "f.close()\nimport hashlib,os\nh=hashlib.sha256(open(%r,'rb').read()).digest()\nprint(binascii.hexlify(h).decode())" % tmp)
    want = hashlib.sha256(data).hexdigest()
    if out.strip() != want:
        sys.exit('hash mismatch %s: %r vs %s %s' % (dev, out, want, err))
    out, err = exec_raw("os.rename(%r,%r)" % (tmp, dev))
    if err.strip():
        sys.exit('rename error %s: %s' % (dev, err))
    print('OK', dev, len(data), want[:12])

s.write(b'import machine\nmachine.reset()\n\x04')
time.sleep(0.5)
print('reset sent')
