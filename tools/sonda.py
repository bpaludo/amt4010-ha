"""Sonda somente-leitura das centrais Intelbras linha v1 (ISECMobile 0xE9).

Uso:  python3 sonda.py <ip> 5A | 5B | 5C <endereço hex 4 díg.> <quantidade hex <= C0>
      python3 sonda.py --selftest

Uma conexão, UM pedido de leitura, fecha — sempre depois de ler a resposta inteira.
Só existem 5A (status parcial), 5B (status completo) e 5C (leitura de EEPROM). Nenhum
comando de arme, desarme, sirene, pânico, bypass ou PGM pode ser montado aqui.
A senha (master ou de USUÁRIO, 4 ou 6 dígitos — a de acesso remoto NÃO vale no 0xE9)
chega por stdin e nunca é impressa.
"""
import socket
import sys

MODELOS = {0x1E: "AMT 2018 E/EG", 0x2E: "AMT 2118 EG", 0x32: "AMT 2018 E3G",
           0x34: "AMT 2018 E Smart", 0x41: "AMT 4010", 0x61: "AMT 1016 NET",
           0x20: "AMT 2110", 0x24: "ANM 24 NET", 0x35: "ELC 3020 NET", 0x36: "AMT 1000 Smart"}
NACKS = {0xE0: "formato inválido", 0xE1: "senha incorreta", 0xE2: "comando inválido",
         0xE3: "central não particionada", 0xE4: "zonas abertas", 0xE5: "comando descontinuado",
         0xE6: "sem permissão (bypass)", 0xE7: "sem permissão (desativar)",
         0xE8: "bypass não permitido com central ativada"}
# status -> (tamanho, índice do modelo, índice do firmware) — SDK Intelbras v1.0.1
LAYOUT = {0x5A: (43, 18, 19), 0x5B: (54, 24, 25)}
LEITURAS = {0x5A, 0x5B, 0x5C}
MAX_5C = 0xC0
# 5C só na faixa dos nomes de zona (mesma allowlist da integração): um endereço
# digitado errado não pode imprimir outra área da EEPROM (senhas) no terminal.
NOMES_INI, NOMES_FIM = 0x0800, 0x0C00


def chk(b):
    x = 0
    for v in b:
        x ^= v
    return x ^ 0xFF


def frame(pw, cmd, content=b""):
    if cmd not in LEITURAS:
        raise ValueError(f"0x{cmd:02X} não é leitura — recusado")
    body = bytes([0xE9, 0x21]) + pw.encode("ascii") + bytes([cmd]) + content + bytes([0x21])
    f = bytes([len(body)]) + body
    return f + bytes([chk(f)])


def args():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    host, cmd = sys.argv[1], int(sys.argv[2], 16)
    content = b""
    if cmd == 0x5C:
        if len(sys.argv) != 5:
            sys.exit("5C exige <endereço> <quantidade>, ex.: 5C 0800 C0")
        addr, qty = int(sys.argv[3], 16), int(sys.argv[4], 16)
        if not (NOMES_INI <= addr and addr + qty <= NOMES_FIM and 1 <= qty <= MAX_5C):
            sys.exit(f"5C só nos nomes de zona: {NOMES_INI:04X}–{NOMES_FIM - 1:04X}, "
                     f"quantidade 01–{MAX_5C:02X} (nada foi enviado)")
        content = bytes([addr >> 8, addr & 0xFF, qty])
    elif cmd not in LAYOUT:
        sys.exit("comando deve ser 5A, 5B ou 5C (nada foi enviado)")
    return host, cmd, content


def main():
    host, cmd, content = args()
    pw = sys.stdin.readline().strip()
    if not (pw.isdigit() and len(pw) in (4, 6)):
        sys.exit("senha deve ter 4 ou 6 dígitos (nada foi enviado)")
    pkt = frame(pw, cmd, content)
    del pw
    buf = b""
    with socket.create_connection((host, 9009), timeout=6) as s:
        s.sendall(pkt)
        try:
            while not buf or len(buf) < buf[0] + 2:
                d = s.recv(512)
                if not d:
                    break
                buf += d
        except socket.timeout:
            pass
    if not buf:
        sys.exit("sem resposta (timeout) — não repetir; investigar antes")
    if len(buf) < buf[0] + 2 or chk(buf[: buf[0] + 1]) != buf[buf[0] + 1]:
        sys.exit(f"resposta truncada ou checksum inválido: {len(buf)} bytes, cabeçalho {buf[:2].hex(' ')}")
    if buf[1] != 0xE9:
        sys.exit(f"resposta inesperada: cmd=0x{buf[1]:02X} ({len(buf)} bytes)")
    if buf[0] == 2:
        code = buf[2]
        if code == 0xFE:
            print("ACK 0xFE")
        else:
            print(f"NACK 0x{code:02X} = {NACKS.get(code, 'desconhecido')}")
            if code == 0xE1:
                print("⚠️  NÃO repetir: confirmar com o cliente qual senha é (master/usuário, 4 ou 6 dígitos).")
        return
    data = buf[2: buf[0] + 1]
    if cmd == 0x5C:
        idx, eeprom = data[0], data[1:]
        print(f"5C OK — índice do usuário {idx}, {len(eeprom)} bytes (pedidos {content[2]})")
        base = (content[0] << 8) | content[1]
        for off in range(0, len(eeprom), 16):
            rec = eeprom[off: off + 16]
            txt = rec.split(b"\x00", 1)[0].decode("latin-1", "replace")
            print(f"  0x{base + off:04X}: {rec.hex(' ')}  |{txt}|")
        return
    size, im, ifw = LAYOUT[cmd]
    if len(data) != size:
        sys.exit(f"0x{cmd:02X}: {len(data)} bytes de status (esperado {size})")
    m, fw = data[im], data[ifw]
    print(f"status 0x{cmd:02X} OK — {size} bytes")
    print(f"modelo   : 0x{m:02X} = {MODELOS.get(m, 'desconhecido')}")
    print(f"firmware : {fw >> 4}.{fw & 0xF}")
    print(f"raw      : {data.hex(' ')}")


def selftest():
    # exemplos da planilha SDK Intelbras v1.0.1, aba "4-Comandar central via APP", senha 1234
    assert frame("1234", 0x5A).hex(" ") == "08 e9 21 31 32 33 34 5a 21 40"
    assert frame("1234", 0x5B).hex(" ") == "08 e9 21 31 32 33 34 5b 21 41"
    assert frame("1234", 0x5C, bytes([0x05, 0x1A, 0x01])).hex(" ") == "0b e9 21 31 32 33 34 5c 05 1a 01 21 5b"
    assert chk(bytes([0x02, 0xE9, 0xFE])) == 0xEA   # ACK do SDK: 02 E9 FE EA
    assert chk(bytes([0x02, 0xE9, 0xE1])) == 0xF5   # NACK capturado em 03/08 e 29/09
    for proibido in (0x41, 0x42, 0x43, 0x44, 0x45, 0x50, 0x63, 0xE7):
        try:
            frame("1234", proibido)
        except ValueError:
            continue
        raise AssertionError(f"0x{proibido:02X} deveria ser recusado")
    print("selftest ok")


if __name__ == "__main__":
    selftest() if sys.argv[1:] == ["--selftest"] else main()
