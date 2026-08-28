#!/usr/bin/env python3
"""
Herramienta de pruebas locales — Subestación YB-6000KVA
Gravitas Sun Harvest, S.A.

Cliente IEC 60870-5-104 + interfaz web local para comisionamiento en campo.
Sin dependencias externas: solo librería estándar de Python 3.8+.

Uso:
    python campo.py                 # abre la interfaz en http://127.0.0.1:8080
    python campo.py --simular       # además levanta un equipo simulado en :2404
    python campo.py --puerto 9000   # cambia el puerto de la interfaz

El simulador permite probar toda la interfaz desde la oficina, antes de llegar
a planta: conéctese a 127.0.0.1 puerto 2404.
"""

import argparse
import csv
import json
import os
import platform
import re
import socket
import struct
import subprocess
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

BASE = os.path.dirname(os.path.abspath(__file__))
DATOS = os.path.join(BASE, "datos")
CONFIG_PATH = os.path.join(BASE, "config.json")

# --------------------------------------------------------------------------
# Configuración por defecto
# --------------------------------------------------------------------------

CONFIG_DEFECTO = {
    "equipos": [
        {
            "id": "nrt",
            "nombre": "NRT-333T (Sala BT)",
            "ip": "172.20.251.88",
            "puerto": 2404,
            "ca": 1,
            "nota": "IEC 60870-5-104 confirmado: puerto 2404 abierto. Dirección común (CA) sin "
                    "confirmar todavía — pruebe 1; si no responde, revise el manual.",
        },
        {
            "id": "p3u30",
            "nombre": "Easergy P3U30 (Sala MT)",
            "ip": "172.20.251.90",
            "puerto": 2404,
            "ca": 1,
            "nota": "IP y protocolo aún no confirmados en campo. Puede requerir configuración por "
                    "USB primero, y verificar si usa IEC 104 u otro protocolo.",
        },
        {
            "id": "sim",
            "nombre": "Simulador local (pruebas de oficina)",
            "ip": "127.0.0.1",
            "puerto": 2404,
            "ca": 1,
            "nota": "Equipo ficticio IEC 104. Sirve para validar la herramienta sin hardware.",
        },
    ],
    "puntos": [
        {"di": 1, "borne": "X6:1", "ioa": 1, "nombre": "Alarma Gas Ligero", "equipo": "Relé QJ4-50", "accion": "alarma"},
        {"di": 2, "borne": "X6:2", "ioa": 2, "nombre": "Disparo Gas Pesado", "equipo": "Relé QJ4-50", "accion": "disparo"},
        {"di": 3, "borne": "X6:3", "ioa": 3, "nombre": "Alarma Alta Temp", "equipo": "Termómetro BWY-802/803", "accion": "alarma"},
        {"di": 4, "borne": "X6:4", "ioa": 4, "nombre": "Disparo Ultra Alta Temp", "equipo": "Termómetro BWY-802/803", "accion": "disparo"},
        {"di": 5, "borne": "X6:5", "ioa": 5, "nombre": "Alarma Nivel Bajo Aceite", "equipo": "Indicador YZF", "accion": "alarma"},
        {"di": 6, "borne": "X6:6", "ioa": 6, "nombre": "Válvula Alivio de Presión", "equipo": "YSF6-55/50 KJ", "accion": "disparo"},
    ],
    "signoff": [
        {"n": 1, "prueba": "Enlace físico Ethernet NRT-333T", "esperado": "LED NET-1 o NET-A parpadea verde"},
        {"n": 2, "prueba": "Ping test IP NRT-333T", "esperado": "Ping exitoso a 172.20.251.88, latencia <5 ms"},
        {"n": 3, "prueba": "Verificación de mapeo bornera X6", "esperado": "Interrogación general IEC 104 devuelve DI 1 a DI 6 con IOA correcto"},
        {"n": 4, "prueba": "Prueba de disparo local P3U30", "esperado": "Apertura física de RMU mediante Forcing Flag T1"},
    ],
}


def cargar_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            for k, v in CONFIG_DEFECTO.items():
                cfg.setdefault(k, v)
            return cfg
        except Exception:
            pass
    guardar_config(CONFIG_DEFECTO)
    return json.loads(json.dumps(CONFIG_DEFECTO))


def guardar_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


# --------------------------------------------------------------------------
# IEC 60870-5-104 — framing APCI y codificación/decodificación de ASDU
# --------------------------------------------------------------------------

class IEC104Error(Exception):
    pass


TIPOS_ASDU = {
    1: ("M_SP_NA_1", "punto simple"),
    3: ("M_DP_NA_1", "punto doble"),
    5: ("M_ST_NA_1", "posición de mando"),
    7: ("M_BO_NA_1", "cadena de bits 32"),
    9: ("M_ME_NA_1", "medida normalizada"),
    11: ("M_ME_NB_1", "medida escalada"),
    13: ("M_ME_NC_1", "medida flotante"),
    15: ("M_IT_NA_1", "contador"),
    30: ("M_SP_TB_1", "punto simple c/hora"),
    31: ("M_DP_TB_1", "punto doble c/hora"),
    32: ("M_ST_TB_1", "posición de mando c/hora"),
    34: ("M_ME_TD_1", "medida normalizada c/hora"),
    35: ("M_ME_TE_1", "medida escalada c/hora"),
    36: ("M_ME_TF_1", "medida flotante c/hora"),
    37: ("M_IT_TB_1", "contador c/hora"),
    100: ("C_IC_NA_1", "interrogación general"),
    102: ("C_RD_NA_1", "lectura puntual"),
    103: ("C_CS_NA_1", "sincronización de hora"),
}

CAUSAS = {
    1: "cíclica", 2: "exploración de fondo", 3: "espontánea", 4: "inicializada",
    5: "solicitada", 6: "activación", 7: "confirmación activación", 8: "desactivación",
    9: "confirmación desactivación", 10: "fin de activación",
    11: "retorno por mando remoto", 12: "retorno por mando local", 13: "transferencia de archivo",
    20: "interrogación general", 44: "TypeID desconocido", 45: "causa desconocida",
    46: "dirección común desconocida", 47: "dirección de objeto desconocida",
}
CAUSAS.update({20 + g: "interrogación de grupo %d" % g for g in range(1, 17)})


def _iec_ioa(ioa):
    return bytes([ioa & 0xFF, (ioa >> 8) & 0xFF, (ioa >> 16) & 0xFF])


def _apdu_u(byte0):
    return bytes([0x68, 4, byte0, 0, 0, 0])


def _apdu_s(nr):
    return bytes([0x68, 4, 0x01, 0]) + struct.pack("<H", (nr & 0x7FFF) << 1)


def _apdu_i(ns, nr, asdu):
    c0 = (ns & 0x7F) << 1
    c1 = (ns >> 7) & 0xFF
    c2 = (nr & 0x7F) << 1
    c3 = (nr >> 7) & 0xFF
    cuerpo = bytes([c0, c1, c2, c3]) + asdu
    return bytes([0x68, len(cuerpo)]) + cuerpo


def _recv_exacto(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise IEC104Error("El equipo cerró la conexión")
        buf += chunk
    return buf


def _apdu_leer(sock):
    """Lee un APDU completo. Devuelve ('I',(ns,nr,asdu)) | ('S',nr) | ('U',byte0)."""
    cab = _recv_exacto(sock, 2)
    if cab[0] != 0x68:
        raise IEC104Error("Trama IEC 104 inválida (falta 0x68 de inicio)")
    largo = cab[1]
    cuerpo = _recv_exacto(sock, largo)
    c = cuerpo[:4]
    if c[0] & 1 == 0:
        ns = (c[0] >> 1) | (c[1] << 7)
        nr = (c[2] >> 1) | (c[3] << 7)
        return "I", (ns, nr, cuerpo[4:])
    elif c[0] & 0x03 == 0x01:
        nr = (c[2] >> 1) | (c[3] << 7)
        return "S", nr
    else:
        return "U", c[0]


def _decode_cp56(b):
    if len(b) < 7:
        return None
    ms = struct.unpack("<H", b[0:2])[0]
    minuto = b[2] & 0x3F
    hora = b[3] & 0x1F
    dia = b[4] & 0x1F
    mes = b[5] & 0x0F
    anio = b[6] & 0x7F
    try:
        return "%04d-%02d-%02d %02d:%02d:%02d.%03d" % (2000 + anio, mes, dia, hora, minuto, ms // 1000, ms % 1000)
    except Exception:
        return None


def _calidad(byte):
    f = []
    if byte & 0x10: f.append("bloqueado")
    if byte & 0x20: f.append("sustituido")
    if byte & 0x40: f.append("no actual")
    if byte & 0x80: f.append("inválido")
    return ", ".join(f) if f else "buena"


def _decodificar_valor(tipo, b):
    """Devuelve (valor, calidad, hora_o_None, bytes_consumidos)."""
    if tipo in (1, 30):
        siq = b[0]
        return siq & 1, _calidad(siq), (_decode_cp56(b[1:8]) if tipo == 30 else None), (8 if tipo == 30 else 1)
    if tipo in (3, 31):
        diq = b[0]
        dpi = diq & 0x03
        valor = {0: "indeterminado", 1: "abierto/OFF", 2: "cerrado/ON", 3: "indeterminado"}[dpi]
        return valor, _calidad(diq), (_decode_cp56(b[1:8]) if tipo == 31 else None), (8 if tipo == 31 else 1)
    if tipo in (5, 32):
        vti = b[0] & 0x7F
        if vti > 63:
            vti -= 128
        return vti, _calidad(b[1]), (_decode_cp56(b[2:9]) if tipo == 32 else None), (9 if tipo == 32 else 2)
    if tipo == 7:
        v = struct.unpack("<I", b[0:4])[0]
        return v, _calidad(b[4]), None, 5
    if tipo in (9, 34):
        v = round(struct.unpack("<h", b[0:2])[0] / 32768.0, 5)
        return v, _calidad(b[2]), (_decode_cp56(b[3:10]) if tipo == 34 else None), (10 if tipo == 34 else 3)
    if tipo in (11, 35):
        v = struct.unpack("<h", b[0:2])[0]
        return v, _calidad(b[2]), (_decode_cp56(b[3:10]) if tipo == 35 else None), (10 if tipo == 35 else 3)
    if tipo in (13, 36):
        v = round(struct.unpack("<f", b[0:4])[0], 4)
        return v, _calidad(b[4]), (_decode_cp56(b[5:12]) if tipo == 36 else None), (12 if tipo == 36 else 5)
    if tipo in (15, 37):
        v = struct.unpack("<i", b[0:4])[0]
        return v, _calidad(b[4]), (_decode_cp56(b[5:12]) if tipo == 37 else None), (12 if tipo == 37 else 5)
    if tipo == 100:
        return b[0], None, None, 1
    if tipo == 102:
        return None, None, None, 0
    return None, None, None, len(b)


def _decodificar_asdu(asdu):
    tipo = asdu[0]
    vsq = asdu[1]
    sq = bool(vsq & 0x80)
    n = vsq & 0x7F
    cot_b0 = asdu[2]
    cot = cot_b0 & 0x3F
    prueba = bool(cot_b0 & 0x80)
    negativo = bool(cot_b0 & 0x40)
    ca = struct.unpack("<H", asdu[4:6])[0]
    resto = asdu[6:]
    nombre_tipo, desc_tipo = TIPOS_ASDU.get(tipo, ("TYPE_%d" % tipo, "desconocido"))
    objetos = []
    off = 0
    ioa_base = None
    for i in range(n):
        if sq:
            if ioa_base is None:
                ioa_base = resto[off] | (resto[off + 1] << 8) | (resto[off + 2] << 16)
                off += 3
            ioa = ioa_base + i
        else:
            ioa = resto[off] | (resto[off + 1] << 8) | (resto[off + 2] << 16)
            off += 3
        valor, calidad, hora, consumido = _decodificar_valor(tipo, resto[off:])
        off += consumido
        objetos.append({
            "tipo": tipo, "tipo_nombre": nombre_tipo, "tipo_desc": desc_tipo,
            "cot": cot, "cot_nombre": CAUSAS.get(cot, "COT %d" % cot),
            "prueba": prueba, "negativo": negativo, "ca": ca, "ioa": ioa,
            "valor": valor, "calidad": calidad, "hora": hora,
        })
    return objetos


def _iec_asdu_sp(ioa, valor, ca, cot):
    return bytes([1, 0x01, cot & 0x3F, 0]) + struct.pack("<H", ca) + _iec_ioa(ioa) + bytes([1 if valor else 0])


def _iec_asdu_float(ioa, valor, ca, cot):
    return bytes([13, 0x01, cot & 0x3F, 0]) + struct.pack("<H", ca) + _iec_ioa(ioa) + struct.pack("<f", valor) + bytes([0])


def _iec_asdu_gi_fin(ca):
    return bytes([100, 0x01, 10, 0]) + struct.pack("<H", ca) + _iec_ioa(0) + bytes([20])


class ClienteIEC104:
    """Maestro IEC 104 mínimo: handshake STARTDT, interrogación general, lectura
    puntual (C_RD_NA_1) y caché de último valor por IOA alimentada por un hilo de
    lectura continua (así se capturan también las actualizaciones espontáneas)."""

    def __init__(self, host, puerto=2404, ca=1, timeout=3.0):
        self.host = host
        self.puerto = int(puerto)
        self.ca = int(ca)
        self.timeout = timeout
        self.sock = None
        self.activo = False
        self.hilo = None
        self._lock_envio = threading.Lock()
        self._cond = threading.Condition()
        self.ns = 0
        self.nr = 0
        self._gi_en_curso = False
        self._gi_terminado = False
        self._gi_buffer = []
        self._lecturas_ioa = {}

    def conectar(self):
        self.cerrar()
        s = socket.create_connection((self.host, self.puerto), timeout=self.timeout)
        s.settimeout(self.timeout)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock = s
        self.ns = 0
        self.nr = 0
        s.sendall(_apdu_u(0x07))  # STARTDT act
        try:
            tipo, _dato = _apdu_leer(s)
        except socket.timeout:
            raise IEC104Error("El equipo no confirmó STARTDT en %.0f s." % self.timeout)
        if tipo != "U":
            raise IEC104Error("Respuesta inesperada al iniciar enlace IEC 104 (esperaba STARTDT con).")
        self.activo = True
        self.hilo = threading.Thread(target=self._bucle_lectura, daemon=True)
        self.hilo.start()

    def cerrar(self):
        self.activo = False
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
        self.sock = None

    def _enviar_i(self, asdu):
        with self._lock_envio:
            self.sock.sendall(_apdu_i(self.ns, self.nr, asdu))
            self.ns = (self.ns + 1) % 0x8000

    def _bucle_lectura(self):
        self.sock.settimeout(1.0)
        while self.activo:
            try:
                tipo, dato = _apdu_leer(self.sock)
            except socket.timeout:
                continue
            except Exception:
                self.activo = False
                break
            if tipo == "I":
                m_ns, _m_nr, asdu_rx = dato
                self.nr = m_ns + 1
                try:
                    with self._lock_envio:
                        self.sock.sendall(_apdu_s(self.nr))
                except Exception:
                    self.activo = False
                    break
                try:
                    objetos = _decodificar_asdu(asdu_rx)
                except Exception:
                    objetos = []
                with self._cond:
                    if self._gi_en_curso:
                        self._gi_buffer.extend(objetos)
                        if any(o["tipo"] == 100 and o["cot"] == 10 for o in objetos):
                            self._gi_terminado = True
                    for o in objetos:
                        self._lecturas_ioa[o["ioa"]] = o
                    self._cond.notify_all()
            elif tipo == "U":
                if dato == 0x43:  # TESTFR act
                    try:
                        with self._lock_envio:
                            self.sock.sendall(_apdu_u(0x83))
                    except Exception:
                        self.activo = False
                        break

    def interrogacion_general(self, espera=5.0):
        with self._cond:
            self._gi_en_curso = True
            self._gi_terminado = False
            self._gi_buffer = []
        asdu = bytes([100, 0x01, 6, 0]) + struct.pack("<H", self.ca) + _iec_ioa(0) + bytes([20])
        self._enviar_i(asdu)
        with self._cond:
            self._cond.wait_for(lambda: self._gi_terminado, timeout=espera)
            objetos = list(self._gi_buffer)
            terminado = self._gi_terminado
            self._gi_en_curso = False
        return objetos, terminado

    def leer_ioa(self, ioa, espera=2.0):
        with self._cond:
            self._lecturas_ioa.pop(ioa, None)
        asdu = bytes([102, 0x01, 5, 0]) + struct.pack("<H", self.ca) + _iec_ioa(ioa)
        self._enviar_i(asdu)
        with self._cond:
            ok = self._cond.wait_for(lambda: ioa in self._lecturas_ioa, timeout=espera)
            if not ok:
                raise IEC104Error("Sin respuesta del equipo para IOA %d en %.1f s." % (ioa, espera))
            return self._lecturas_ioa[ioa]

    def valor_actual(self, ioa):
        with self._cond:
            return self._lecturas_ioa.get(ioa)


# --------------------------------------------------------------------------
# Diagnóstico de red
# --------------------------------------------------------------------------

def probar_puerto_tcp(host, puerto, timeout=2.0):
    t0 = time.perf_counter()
    try:
        s = socket.create_connection((host, int(puerto)), timeout=timeout)
        s.close()
        return {"ok": True, "ms": round((time.perf_counter() - t0) * 1000, 2)}
    except Exception as e:
        return {"ok": False, "ms": None, "error": str(e)}


def ping(host, n=4):
    if platform.system().lower().startswith("win"):
        cmd = ["ping", "-n", str(n), "-w", "2000", host]
    else:
        cmd = ["ping", "-c", str(n), "-W", "2", host]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=n * 3 + 6)
        salida = p.stdout + p.stderr
    except Exception as e:
        return {"ok": False, "latencias": [], "salida": str(e), "promedio": None}
    lat = [float(x) for x in re.findall(r"(?:time|tiempo)[=<]\s*([\d.,]+)\s*ms", salida.replace(",", "."))]
    prom = round(sum(lat) / len(lat), 2) if lat else None
    return {
        "ok": bool(lat),
        "latencias": lat,
        "promedio": prom,
        "salida": salida.strip(),
        "cumple_5ms": bool(prom is not None and prom < 5.0),
    }


# --------------------------------------------------------------------------
# Registro de datos (CSV)
# --------------------------------------------------------------------------

_lock_csv = threading.Lock()


def _archivo(nombre):
    os.makedirs(DATOS, exist_ok=True)
    return os.path.join(DATOS, "%s_%s.csv" % (nombre, datetime.now().strftime("%Y%m%d")))


def registrar(nombre, cabecera, fila):
    with _lock_csv:
        ruta = _archivo(nombre)
        nuevo = not os.path.exists(ruta)
        with open(ruta, "a", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            if nuevo:
                w.writerow(cabecera)
            w.writerow(fila)
    return os.path.basename(ruta)


def ahora():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


CAB_LECTURA = ["timestamp", "equipo", "ip", "ioa", "tipo", "valor", "calidad", "ms", "nota"]
CAB_EVENTO = ["timestamp", "di", "borne", "senal", "equipo_campo", "de", "a", "accion", "ioa"]
CAB_NOTA = ["timestamp", "categoria", "texto"]
CAB_PRUEBA = ["timestamp", "ip", "puerto", "ca", "resultado", "detalle"]


# --------------------------------------------------------------------------
# Estado en memoria
# --------------------------------------------------------------------------

class Estado:
    def __init__(self):
        self.cfg = cargar_config()
        self.cliente = None
        self.equipo = None
        self.ultimo_di = None
        self.snapshot_a = None
        self.snapshot_b = None
        self.eventos = []

    def conectar(self, ip, puerto, ca):
        if self.cliente:
            self.cliente.cerrar()
        self.cliente = ClienteIEC104(ip, puerto, ca)
        self.cliente.conectar()
        self.equipo = {"ip": ip, "puerto": int(puerto), "ca": int(ca)}
        self.ultimo_di = None
        return self.equipo


EST = Estado()


def _es_activo(obj):
    if obj is None:
        return False
    if obj["tipo"] in (1, 30):
        return bool(obj["valor"])
    if obj["tipo"] in (3, 31):
        return obj["valor"] == "cerrado/ON"
    return bool(obj["valor"])


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

def api_conectar(d):
    ip = (d.get("ip") or "").strip()
    if not ip:
        raise IEC104Error("Escriba la dirección IP del equipo.")
    puerto = d.get("puerto", 2404)
    ca = int(d.get("ca", 1))
    try:
        eq = EST.conectar(ip, puerto, ca)
    except socket.timeout:
        raise IEC104Error(
            "%s no respondió en 3 s. Revise el LED de enlace del puerto RJ45 y que la laptop "
            "tenga IP estática en la misma red (172.20.251.100 / 255.255.0.0)." % ip)
    except ConnectionRefusedError:
        raise IEC104Error(
            "%s está en la red pero rechazó el puerto %s. Confirme que el servicio IEC 104 esté "
            "activo y use ese puerto." % (ip, puerto))
    except IEC104Error:
        raise
    except OSError as e:
        raise IEC104Error("No se pudo abrir el enlace con %s: %s" % (ip, e))
    return {"ok": True, "equipo": eq}


def _exigir_cliente():
    if EST.cliente is None:
        raise IEC104Error("No hay conexión activa. Conéctese a un equipo primero.")


def api_leer(d):
    """Lectura puntual de un IOA mediante C_RD_NA_1."""
    _exigir_cliente()
    ioa = int(d.get("ioa", 0))
    t0 = time.perf_counter()
    obj = EST.cliente.leer_ioa(ioa, espera=float(d.get("espera", 2.0)))
    ms = round((time.perf_counter() - t0) * 1000, 2)
    if d.get("registrar", True):
        registrar("lecturas", CAB_LECTURA, [
            ahora(), d.get("etiqueta", ""), EST.equipo["ip"], ioa,
            obj["tipo_nombre"], obj["valor"], obj["calidad"], ms, d.get("nota", ""),
        ])
    return {"ok": True, "objeto": obj, "ms": ms}


def api_explorar(d):
    """Interrogación general completa: vuelca todo lo que reporte el equipo."""
    _exigir_cliente()
    objetos, terminado = EST.cliente.interrogacion_general(espera=float(d.get("espera", 5.0)))
    filas = [o for o in objetos if o["tipo"] != 100]
    return {"ok": True, "filas": filas, "terminado": terminado, "cantidad": len(filas)}


def api_snapshot(d):
    """Captura una interrogación general completa para comparar antes/después."""
    _exigir_cliente()
    objetos, _terminado = EST.cliente.interrogacion_general(espera=float(d.get("espera", 5.0)))
    mapa = {o["ioa"]: o for o in objetos if o["tipo"] != 100}
    cual = d.get("cual", "a")
    paquete = {"valores": mapa, "hora": ahora()}
    if cual == "a":
        EST.snapshot_a = paquete
    else:
        EST.snapshot_b = paquete
    return {"ok": True, "cual": cual, "cantidad": len(mapa), "hora": paquete["hora"]}


def api_comparar(_d):
    a, b = EST.snapshot_a, EST.snapshot_b
    if not a or not b:
        raise IEC104Error("Faltan capturas. Tome la captura A, accione la señal en bornera y luego la captura B.")
    cambios = []
    for ioa, oa in a["valores"].items():
        ob = b["valores"].get(ioa)
        if ob is None or ob["valor"] == oa["valor"]:
            continue
        cambios.append({
            "ioa": ioa, "tipo_nombre": oa["tipo_nombre"],
            "antes": oa["valor"], "despues": ob["valor"],
        })
    cambios.sort(key=lambda c: c["ioa"])
    for c in cambios:
        registrar("hallazgos", ["timestamp", "ioa", "tipo", "antes", "despues"],
                  [ahora(), c["ioa"], c["tipo_nombre"], c["antes"], c["despues"]])
    return {"ok": True, "cambios": cambios, "hora_a": a["hora"], "hora_b": b["hora"]}


def api_di(d):
    """Estado de la bornera X6: lee de la caché viva del cliente (alimentada por
    interrogación general y por actualizaciones espontáneas), sin volver a tocar
    la red en cada tick de monitoreo."""
    _exigir_cliente()
    if d.get("interrogar"):
        EST.cliente.interrogacion_general(espera=float(d.get("espera", 4.0)))
    puntos = []
    cambios = []
    for p in EST.cfg["puntos"]:
        ioa = int(p["ioa"])
        obj = EST.cliente.valor_actual(ioa)
        v = 1 if _es_activo(obj) else 0
        anterior = None if EST.ultimo_di is None else EST.ultimo_di.get(str(ioa))
        if anterior is not None and anterior != v:
            ev = {
                "hora": ahora(), "di": p["di"], "borne": p["borne"], "nombre": p["nombre"],
                "de": anterior, "a": v, "accion": p["accion"],
            }
            cambios.append(ev)
            EST.eventos.insert(0, ev)
            del EST.eventos[200:]
            registrar("eventos", CAB_EVENTO, [
                ev["hora"], p["di"], p["borne"], p["nombre"], p["equipo"], anterior, v, p["accion"], ioa,
            ])
        puntos.append(dict(p, valor=v, anterior=anterior,
                            calidad=(obj["calidad"] if obj else "sin datos"),
                            hora=(obj["hora"] if obj else None)))
    EST.ultimo_di = {str(int(p["ioa"])): (1 if _es_activo(EST.cliente.valor_actual(int(p["ioa"]))) else 0)
                      for p in EST.cfg["puntos"]}
    return {"ok": True, "puntos": puntos, "cambios": cambios}


def api_nota(d):
    archivo = registrar("bitacora", CAB_NOTA, [ahora(), d.get("categoria", "nota"), d.get("texto", "")])
    return {"ok": True, "archivo": archivo}


def api_signoff(d):
    filas = d.get("filas", [])
    ruta = os.path.join(DATOS, "signoff_%s.csv" % datetime.now().strftime("%Y%m%d_%H%M"))
    os.makedirs(DATOS, exist_ok=True)
    with open(ruta, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["#", "Procedimiento", "Resultado esperado", "Estado", "Iniciales", "Observaciones", "Hora"])
        for r in filas:
            w.writerow([r.get("n"), r.get("prueba"), r.get("esperado"), r.get("estado"),
                        r.get("iniciales"), r.get("obs"), r.get("hora") or ahora()])
    EST.cfg["signoff_estado"] = filas
    guardar_config(EST.cfg)
    return {"ok": True, "archivo": os.path.basename(ruta)}


def api_config(d):
    if d.get("guardar"):
        for k in ("puntos", "equipos"):
            if k in d:
                EST.cfg[k] = d[k]
        guardar_config(EST.cfg)
    return {"ok": True, "config": EST.cfg, "conectado": EST.equipo, "carpeta_datos": DATOS}


def api_ping(d):
    r = ping(d.get("ip", ""), int(d.get("n", 4)))
    r["puerto"] = probar_puerto_tcp(d.get("ip", ""), d.get("puerto", 2404))
    registrar("lecturas", CAB_LECTURA, [
        ahora(), "ping", d.get("ip", ""), "-", "-",
        "prom=%s ms" % r.get("promedio"), "-", r.get("promedio") or "", "diagnóstico de red",
    ])
    return {"ok": True, "resultado": r}


def _registrar_prueba(ip, puerto, ca, pasos):
    resumen = " | ".join("%s:%s" % (p["id"], "OK" if p["ok"] else "FALLO") for p in pasos)
    todo_ok = all(p["ok"] for p in pasos)
    registrar("pruebas", CAB_PRUEBA, [ahora(), ip, puerto, ca, "OK" if todo_ok else "FALLAS", resumen])
    return todo_ok


def api_probar_todo(d):
    """Batería completa para un solo click en campo: red, enlace IEC 104 e
    interrogación general contra la bornera X6 configurada. Pensado principalmente
    para el NRT-333T."""
    ip = (d.get("ip") or "").strip()
    if not ip:
        raise IEC104Error("Escriba la dirección IP del equipo.")
    puerto = int(d.get("puerto", 2404))
    ca = int(d.get("ca", 1))
    pasos = []

    r_ping = ping(ip)
    pasos.append({
        "id": "ping", "ok": r_ping["ok"],
        "detalle": ("Responde, promedio %s ms." % r_ping["promedio"]) if r_ping["ok"]
        else "Sin respuesta ICMP — revise el LED NET-1/NET-A y el cable Cat 6.",
    })

    r_puerto = probar_puerto_tcp(ip, puerto)
    pasos.append({
        "id": "puerto", "ok": r_puerto["ok"],
        "detalle": ("Abierto en %s ms." % r_puerto["ms"]) if r_puerto["ok"]
        else "Puerto %s cerrado o filtrado: %s" % (puerto, r_puerto.get("error", "")),
    })

    try:
        r_con = api_conectar({"ip": ip, "puerto": puerto, "ca": ca})
        pasos.append({"id": "conectar", "ok": True, "detalle": "STARTDT confirmado, enlace IEC 104 activo."})
    except IEC104Error as e:
        pasos.append({"id": "conectar", "ok": False, "detalle": str(e)})
        todo_ok = _registrar_prueba(ip, puerto, ca, pasos)
        return {"ok": True, "pasos": pasos, "todo_ok": todo_ok, "equipo": None}

    try:
        objetos, terminado = EST.cliente.interrogacion_general(espera=float(d.get("espera", 5.0)))
        objetos = [o for o in objetos if o["tipo"] != 100]
        ioas_cfg = {int(p["ioa"]) for p in EST.cfg["puntos"]}
        encontrados = sum(1 for o in objetos if o["ioa"] in ioas_cfg)
        detalle = "%d objetos recibidos (%d/%d puntos de bornera X6 encontrados)%s." % (
            len(objetos), encontrados, len(ioas_cfg), "" if terminado else " — sin fin de activación, revise el CA")
        pasos.append({"id": "interrogacion", "ok": terminado and len(objetos) > 0, "detalle": detalle})
    except IEC104Error as e:
        pasos.append({"id": "interrogacion", "ok": False, "detalle": str(e)})

    todo_ok = _registrar_prueba(ip, puerto, ca, pasos)
    return {"ok": True, "pasos": pasos, "todo_ok": todo_ok, "equipo": r_con["equipo"]}


def api_archivos(_d):
    os.makedirs(DATOS, exist_ok=True)
    items = []
    for n in sorted(os.listdir(DATOS)):
        ruta = os.path.join(DATOS, n)
        items.append({"nombre": n, "bytes": os.path.getsize(ruta),
                      "hora": datetime.fromtimestamp(os.path.getmtime(ruta)).strftime("%H:%M:%S")})
    return {"ok": True, "carpeta": DATOS, "archivos": items}


RUTAS = {
    "/api/conectar": api_conectar,
    "/api/leer": api_leer,
    "/api/explorar": api_explorar,
    "/api/snapshot": api_snapshot,
    "/api/comparar": api_comparar,
    "/api/di": api_di,
    "/api/nota": api_nota,
    "/api/signoff": api_signoff,
    "/api/config": api_config,
    "/api/ping": api_ping,
    "/api/probar-todo": api_probar_todo,
    "/api/archivos": api_archivos,
}


# --------------------------------------------------------------------------
# Servidor HTTP
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_a):
        pass

    def _enviar(self, cuerpo, tipo="application/json; charset=utf-8", codigo=200):
        if isinstance(cuerpo, str):
            cuerpo = cuerpo.encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(cuerpo)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(cuerpo)

    def do_GET(self):
        ruta = urlparse(self.path).path
        if ruta in ("/", "/index.html"):
            try:
                with open(os.path.join(BASE, "ui.html"), "rb") as f:
                    return self._enviar(f.read(), "text/html; charset=utf-8")
            except FileNotFoundError:
                return self._enviar("Falta ui.html junto a campo.py", "text/plain; charset=utf-8", 500)
        if ruta.startswith("/datos/"):
            nombre = os.path.basename(ruta)
            destino = os.path.join(DATOS, nombre)
            if os.path.isfile(destino):
                with open(destino, "rb") as f:
                    return self._enviar(f.read(), "text/csv; charset=utf-8")
            return self._enviar("No encontrado", "text/plain; charset=utf-8", 404)
        return self._enviar("No encontrado", "text/plain; charset=utf-8", 404)

    def do_POST(self):
        ruta = urlparse(self.path).path
        fn = RUTAS.get(ruta)
        if not fn:
            return self._enviar(json.dumps({"ok": False, "error": "Ruta desconocida"}), codigo=404)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            datos = json.loads(self.rfile.read(n) or b"{}")
            return self._enviar(json.dumps(fn(datos), ensure_ascii=False))
        except IEC104Error as e:
            return self._enviar(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        except Exception as e:
            return self._enviar(json.dumps({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}, ensure_ascii=False))


# --------------------------------------------------------------------------
# Simulador de equipo IEC 104 (para probar sin hardware)
# --------------------------------------------------------------------------

def _sim_enviar(conn, asdu):
    with conn["lock"]:
        conn["sock"].sendall(_apdu_i(conn["ns"], conn["nr"], asdu))
        conn["ns"] = (conn["ns"] + 1) % 0x8000


def _manejar_cliente_iec104(sim, conn):
    sock = conn["sock"]
    try:
        while True:
            tipo, dato = _apdu_leer(sock)
            if tipo == "U":
                if dato == 0x07:
                    sock.sendall(_apdu_u(0x0B))
                elif dato == 0x43:
                    sock.sendall(_apdu_u(0x83))
            elif tipo == "I":
                m_ns, _m_nr, asdu_rx = dato
                with conn["lock"]:
                    conn["nr"] = m_ns + 1
                    sock.sendall(_apdu_s(conn["nr"]))
                objetos = _decodificar_asdu(asdu_rx)
                for o in objetos:
                    if o["tipo"] == 100:
                        with sim.lock:
                            estado = dict(sim.estado)
                            medida = sim.medida
                        for ioa, v in sorted(estado.items()):
                            _sim_enviar(conn, _iec_asdu_sp(ioa, v, sim.ca, 20))
                        _sim_enviar(conn, _iec_asdu_float(201, medida, sim.ca, 20))
                        _sim_enviar(conn, _iec_asdu_gi_fin(sim.ca))
                    elif o["tipo"] == 102:
                        ioa = o["ioa"]
                        with sim.lock:
                            if ioa in sim.estado:
                                asdu = _iec_asdu_sp(ioa, sim.estado[ioa], sim.ca, 5)
                            elif ioa == 201:
                                asdu = _iec_asdu_float(201, sim.medida, sim.ca, 5)
                            else:
                                asdu = None
                        if asdu:
                            _sim_enviar(conn, asdu)
            elif tipo == "S":
                pass
    except Exception:
        pass
    finally:
        with sim.lock:
            if conn in sim.clientes:
                sim.clientes.remove(conn)
        try:
            sock.close()
        except Exception:
            pass


class SimuladorIEC104(threading.Thread):
    """Esclavo IEC 60870-5-104 mínimo. Responde interrogación general con 6 puntos
    simples (IOA 101-106, bornera X6) y una medida flotante (IOA 201), y manda
    actualizaciones espontáneas cada 6 s."""

    daemon = True

    def __init__(self, puerto=2404, ca=1):
        super().__init__()
        self.puerto = puerto
        self.ca = ca
        self.estado = {101: 0, 102: 0, 103: 0, 104: 0, 105: 0, 106: 0}
        self.medida = 13800.0
        self.clientes = []
        self.lock = threading.Lock()
        threading.Thread(target=self._animar, daemon=True).start()

    def _animar(self):
        paso = 0
        while True:
            time.sleep(6)
            with self.lock:
                for k in self.estado:
                    self.estado[k] = 0
                paso = (paso + 1) % 7
                ioa_activo = 100 + paso if paso else 101
                if paso:
                    self.estado[ioa_activo] = 1
                self.medida = 13800 + paso * 7
                clientes = list(self.clientes)
                ca = self.ca
                valor = self.estado[ioa_activo]
            for conn in clientes:
                try:
                    _sim_enviar(conn, _iec_asdu_sp(ioa_activo, valor, ca, 3))
                except Exception:
                    pass

    def run(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", self.puerto))
        srv.listen(5)
        while True:
            sock, _addr = srv.accept()
            conn = {"sock": sock, "ns": 0, "nr": 0, "lock": threading.Lock()}
            with self.lock:
                self.clientes.append(conn)
            threading.Thread(target=_manejar_cliente_iec104, args=(self, conn), daemon=True).start()


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Pruebas locales — Subestación YB-6000KVA")
    ap.add_argument("--puerto", type=int, default=8080, help="puerto de la interfaz web")
    ap.add_argument("--simular", action="store_true", help="levantar equipo IEC 104 simulado en 127.0.0.1:2404")
    args = ap.parse_args()

    os.makedirs(DATOS, exist_ok=True)
    if args.simular:
        SimuladorIEC104(2404).start()
        print("Simulador IEC 104 activo en 127.0.0.1:2404")

    srv = ThreadingHTTPServer(("127.0.0.1", args.puerto), Handler)
    print("Interfaz de campo:  http://127.0.0.1:%d" % args.puerto)
    print("Datos guardados en: %s" % DATOS)
    print("Ctrl+C para cerrar.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nCerrado.")


if __name__ == "__main__":
    main()
