#!/usr/bin/env python3
"""
Herramienta de pruebas locales — Subestación YB-6000KVA
Gravitas Sun Harvest, S.A.

Cliente Modbus TCP + interfaz web local para comisionamiento en campo.
Sin dependencias externas: solo librería estándar de Python 3.8+.

Uso:
    python campo.py                 # abre la interfaz en http://127.0.0.1:8080
    python campo.py --simular       # además levanta un equipo simulado en :5502
    python campo.py --puerto 9000   # cambia el puerto de la interfaz

El simulador permite probar toda la interfaz desde la oficina, antes de llegar
a planta: conéctese a 127.0.0.1 puerto 5502.
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
# Configuración por defecto (tomada del manual de comisionamiento)
# --------------------------------------------------------------------------

CONFIG_DEFECTO = {
    "equipos": [
        {
            "id": "nrt",
            "nombre": "NRT-333T (Sala BT)",
            "ip": "172.20.251.88",
            "puerto": 502,
            "unit_id": 1,
            "nota": "Enlace Modbus TCP principal. Laptop en 172.20.251.100/16.",
        },
        {
            "id": "p3u30",
            "nombre": "Easergy P3U30 (Sala MT)",
            "ip": "172.20.251.90",
            "puerto": 502,
            "unit_id": 1,
            "nota": "IP sugerida, aún no confirmada en campo. Puede requerir configuración por USB primero.",
        },
        {
            "id": "sim",
            "nombre": "Simulador local (pruebas de oficina)",
            "ip": "127.0.0.1",
            "puerto": 5502,
            "unit_id": 1,
            "nota": "Equipo ficticio. Sirve para validar la herramienta sin hardware.",
        },
    ],
    "fuente_di": {"fc": 3, "direccion": 55, "base": "1-based"},
    "puntos": [
        {"di": 1, "borne": "X6:1", "bit": 0, "nombre": "Alarma Gas Ligero", "equipo": "Relé QJ4-50", "accion": "alarma"},
        {"di": 2, "borne": "X6:2", "bit": 1, "nombre": "Disparo Gas Pesado", "equipo": "Relé QJ4-50", "accion": "disparo"},
        {"di": 3, "borne": "X6:3", "bit": 2, "nombre": "Alarma Alta Temp", "equipo": "Termómetro BWY-802", "accion": "alarma"},
        {"di": 4, "borne": "X6:4", "bit": 3, "nombre": "Disparo Ultra Alta Temp", "equipo": "BWY-802", "accion": "disparo"},
        {"di": 5, "borne": "X6:5", "bit": 4, "nombre": "Alarma Nivel Bajo Aceite", "equipo": "ZF Gauge", "accion": "alarma"},
        {"di": 6, "borne": "X6:6", "bit": 5, "nombre": "Válvula Alivio de Presión", "equipo": "YSF6-55/50", "accion": "disparo"},
    ],
    "signoff": [
        {"n": 1, "prueba": "Enlace físico Ethernet NRT-333T", "esperado": "LED NET-1 o NET-A parpadea verde"},
        {"n": 2, "prueba": "Ping test IP NRT-333T", "esperado": "Ping exitoso a 172.20.251.88, latencia <5 ms"},
        {"n": 3, "prueba": "Verificación de mapeo bornera X6", "esperado": "Lectura correcta de DI 1 a DI 6 en Modbus TCP"},
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
# Cliente Modbus TCP (stdlib)
# --------------------------------------------------------------------------

class ModbusError(Exception):
    pass


EXCEPCIONES = {
    1: "Función no soportada por el equipo",
    2: "Dirección de registro inválida (fuera de rango)",
    3: "Valor inválido",
    4: "Falla interna del equipo esclavo",
    5: "Petición aceptada, en proceso",
    6: "Equipo ocupado",
    10: "Gateway: ruta no disponible",
    11: "Gateway: el equipo destino no respondió",
}


class ClienteModbus:
    """Cliente Modbus TCP mínimo, con reconexión automática."""

    def __init__(self, host, puerto=502, unit_id=1, timeout=3.0):
        self.host = host
        self.puerto = int(puerto)
        self.unit_id = int(unit_id)
        self.timeout = timeout
        self.sock = None
        self.tid = 0
        self.lock = threading.Lock()

    def conectar(self):
        self.cerrar()
        s = socket.create_connection((self.host, self.puerto), timeout=self.timeout)
        s.settimeout(self.timeout)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock = s

    def cerrar(self):
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
        self.sock = None

    def _recv_exacto(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ModbusError("El equipo cerró la conexión")
            buf += chunk
        return buf

    def _transaccion(self, funcion, payload):
        with self.lock:
            for intento in (1, 2):
                try:
                    if self.sock is None:
                        self.conectar()
                    self.tid = (self.tid + 1) % 0xFFFF
                    pdu = bytes([funcion]) + payload
                    mbap = struct.pack(">HHHB", self.tid, 0, len(pdu) + 1, self.unit_id)
                    t0 = time.perf_counter()
                    self.sock.sendall(mbap + pdu)
                    cab = self._recv_exacto(7)
                    _tid, _proto, largo, _uid = struct.unpack(">HHHB", cab)
                    cuerpo = self._recv_exacto(largo - 1)
                    ms = (time.perf_counter() - t0) * 1000.0
                    fn = cuerpo[0]
                    if fn & 0x80:
                        cod = cuerpo[1]
                        raise ModbusError(
                            "Excepción Modbus %d: %s" % (cod, EXCEPCIONES.get(cod, "desconocida"))
                        )
                    return cuerpo[1:], ms
                except ModbusError:
                    raise
                except (socket.timeout, OSError) as e:
                    self.cerrar()
                    if intento == 2:
                        raise ModbusError("Sin respuesta de %s:%d — %s" % (self.host, self.puerto, e))
        raise ModbusError("Falla de transacción")

    def leer_registros(self, funcion, direccion, cantidad):
        """funcion: 3 (holding) o 4 (input). Devuelve (lista_de_enteros, ms)."""
        datos, ms = self._transaccion(funcion, struct.pack(">HH", direccion, cantidad))
        n = datos[0]
        vals = list(struct.unpack(">" + "H" * (n // 2), datos[1: 1 + n]))
        return vals, ms

    def leer_bits(self, funcion, direccion, cantidad):
        """funcion: 1 (coils) o 2 (discrete inputs). Devuelve (lista_de_0/1, ms)."""
        datos, ms = self._transaccion(funcion, struct.pack(">HH", direccion, cantidad))
        n = datos[0]
        crudo = datos[1: 1 + n]
        bits = []
        for i in range(cantidad):
            bits.append((crudo[i // 8] >> (i % 8)) & 1)
        return bits, ms

    def leer(self, funcion, direccion, cantidad):
        if funcion in (1, 2):
            return self.leer_bits(funcion, direccion, cantidad)
        return self.leer_registros(funcion, direccion, cantidad)

    def escribir_registro(self, direccion, valor):
        _d, ms = self._transaccion(6, struct.pack(">HH", direccion, valor & 0xFFFF))
        return ms

    def escribir_coil(self, direccion, valor):
        _d, ms = self._transaccion(5, struct.pack(">HH", direccion, 0xFF00 if valor else 0x0000))
        return ms


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
    """Ping ICMP del sistema. Devuelve latencias y salida cruda."""
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


CAB_LECTURA = ["timestamp", "equipo", "ip", "fc", "direccion", "cantidad", "valores", "ms", "nota"]
CAB_EVENTO = ["timestamp", "di", "borne", "senal", "equipo_campo", "de", "a", "accion", "registro", "bit"]
CAB_NOTA = ["timestamp", "categoria", "texto"]


# --------------------------------------------------------------------------
# Estado en memoria
# --------------------------------------------------------------------------

class Estado:
    def __init__(self):
        self.cfg = cargar_config()
        self.cliente = None
        self.equipo = None
        self.ultimo_di = None          # dict bit -> valor
        self.snapshot_a = None         # para el comparador
        self.snapshot_b = None
        self.eventos = []              # cola para la UI
        self.escritura_habilitada = False

    def conectar(self, ip, puerto, unit_id):
        if self.cliente:
            self.cliente.cerrar()
        self.cliente = ClienteModbus(ip, puerto, unit_id)
        self.cliente.conectar()
        self.equipo = {"ip": ip, "puerto": int(puerto), "unit_id": int(unit_id)}
        self.ultimo_di = None
        return self.equipo


EST = Estado()


def direccion_efectiva(direccion, base):
    """Modbus se documenta indistintamente 0-based o 1-based. Aquí se normaliza."""
    return max(0, int(direccion) - 1) if base == "1-based" else int(direccion)


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

def api_conectar(d):
    ip = (d.get("ip") or "").strip()
    if not ip:
        raise ModbusError("Escriba la dirección IP del equipo.")
    try:
        eq = EST.conectar(ip, d.get("puerto", 502), d.get("unit_id", 1))
    except socket.timeout:
        raise ModbusError(
            "%s no respondió en 3 s. Revise el LED de enlace del puerto RJ45 y que la laptop "
            "tenga IP estática en la misma red (172.20.251.100 / 255.255.0.0)." % ip)
    except ConnectionRefusedError:
        raise ModbusError(
            "%s está en la red pero rechazó el puerto %s. El equipo responde a ping pero su "
            "servicio Modbus TCP no está activo o usa otro puerto." % (ip, d.get("puerto", 502)))
    except OSError as e:
        raise ModbusError("No se pudo abrir el enlace con %s: %s" % (ip, e))
    return {"ok": True, "equipo": eq}

def _exigir_cliente():
    if EST.cliente is None:
        raise ModbusError("No hay conexión activa. Conéctese a un equipo primero.")


def api_leer(d):
    _exigir_cliente()
    fc = int(d.get("fc", 3))
    direccion = direccion_efectiva(d.get("direccion", 0), d.get("base", "0-based"))
    cantidad = max(1, min(125, int(d.get("cantidad", 1))))
    vals, ms = EST.cliente.leer(fc, direccion, cantidad)
    if d.get("registrar", True):
        registrar("lecturas", CAB_LECTURA, [
            ahora(), d.get("etiqueta", ""), EST.equipo["ip"], fc, direccion,
            cantidad, " ".join(str(v) for v in vals), round(ms, 2), d.get("nota", ""),
        ])
    return {"ok": True, "valores": vals, "ms": round(ms, 2), "direccion_usada": direccion}


def api_escanear(d):
    """Barre un rango en bloques. Sirve para encontrar dónde vive realmente el dato."""
    _exigir_cliente()
    fc = int(d.get("fc", 3))
    inicio = int(d.get("inicio", 0))
    fin = int(d.get("fin", 120))
    bloque = 8 if fc in (3, 4) else 64
    filas, errores = [], []
    dirn = inicio
    t0 = time.perf_counter()
    while dirn <= fin:
        cant = min(bloque, fin - dirn + 1)
        try:
            vals, _ms = EST.cliente.leer(fc, dirn, cant)
            for i, v in enumerate(vals):
                filas.append({"dir": dirn + i, "valor": v})
        except ModbusError as e:
            errores.append({"dir": dirn, "cant": cant, "error": str(e)})
        dirn += cant
    return {
        "ok": True,
        "fc": fc,
        "filas": filas,
        "errores": errores,
        "ms": round((time.perf_counter() - t0) * 1000, 1),
    }


def api_snapshot(d):
    """Captura un rango completo para comparar antes/después de accionar en bornera."""
    res = api_escanear(d)
    cual = d.get("cual", "a")
    mapa = {f["dir"]: f["valor"] for f in res["filas"]}
    paquete = {"fc": res["fc"], "valores": mapa, "hora": ahora()}
    if cual == "a":
        EST.snapshot_a = paquete
    else:
        EST.snapshot_b = paquete
    return {"ok": True, "cual": cual, "cantidad": len(mapa), "hora": paquete["hora"], "errores": res["errores"]}


def api_comparar(_d):
    a, b = EST.snapshot_a, EST.snapshot_b
    if not a or not b:
        raise ModbusError("Faltan capturas. Tome la captura A, accione la señal en bornera y luego la captura B.")
    cambios = []
    for dirn, va in a["valores"].items():
        vb = b["valores"].get(dirn)
        if vb is None or vb == va:
            continue
        bits = [i for i in range(16) if ((va >> i) & 1) != ((vb >> i) & 1)]
        cambios.append({
            "dir": dirn, "antes": va, "despues": vb, "bits": bits,
            "bits_texto": ", ".join("bit %d: %d→%d" % (i, (va >> i) & 1, (vb >> i) & 1) for i in bits),
        })
    cambios.sort(key=lambda c: c["dir"])
    for c in cambios:
        registrar("hallazgos", ["timestamp", "fc", "direccion", "antes", "despues", "bits"],
                  [ahora(), a["fc"], c["dir"], c["antes"], c["despues"], c["bits_texto"]])
    return {"ok": True, "cambios": cambios, "hora_a": a["hora"], "hora_b": b["hora"], "fc": a["fc"]}


def api_di(d):
    """Lee el registro de alarmas y descompone los bits según la configuración."""
    _exigir_cliente()
    fuente = EST.cfg["fuente_di"]
    fc = int(d.get("fc", fuente["fc"]))
    base = d.get("base", fuente["base"])
    direccion = direccion_efectiva(d.get("direccion", fuente["direccion"]), base)
    if fc in (1, 2):
        bits, ms = EST.cliente.leer_bits(fc, direccion, 16)
        crudo = sum(b << i for i, b in enumerate(bits))
    else:
        vals, ms = EST.cliente.leer_registros(fc, direccion, 1)
        crudo = vals[0]
    puntos = []
    cambios = []
    for p in EST.cfg["puntos"]:
        v = (crudo >> int(p["bit"])) & 1
        anterior = None if EST.ultimo_di is None else EST.ultimo_di.get(str(p["bit"]))
        if anterior is not None and anterior != v:
            ev = {
                "hora": ahora(), "di": p["di"], "borne": p["borne"], "nombre": p["nombre"],
                "de": anterior, "a": v, "accion": p["accion"],
            }
            cambios.append(ev)
            EST.eventos.insert(0, ev)
            del EST.eventos[200:]
            registrar("eventos", CAB_EVENTO, [
                ev["hora"], p["di"], p["borne"], p["nombre"], p["equipo"],
                anterior, v, p["accion"], direccion, p["bit"],
            ])
        puntos.append(dict(p, valor=v, anterior=anterior))
    EST.ultimo_di = {str(p["bit"]): (crudo >> int(p["bit"])) & 1 for p in EST.cfg["puntos"]}
    return {
        "ok": True, "crudo": crudo, "binario": format(crudo, "016b"), "ms": round(ms, 2),
        "puntos": puntos, "cambios": cambios, "direccion_usada": direccion, "fc": fc,
    }


def api_escribir(d):
    if not EST.escritura_habilitada:
        raise ModbusError("Modo escritura bloqueado. Actívelo con el interruptor de la pestaña Escritura.")
    _exigir_cliente()
    fc = int(d.get("fc", 6))
    direccion = direccion_efectiva(d.get("direccion", 0), d.get("base", "0-based"))
    valor = int(d.get("valor", 0))
    ms = EST.cliente.escribir_coil(direccion, valor) if fc == 5 else EST.cliente.escribir_registro(direccion, valor)
    registrar("escrituras", ["timestamp", "ip", "fc", "direccion", "valor", "ms"],
              [ahora(), EST.equipo["ip"], fc, direccion, valor, round(ms, 2)])
    return {"ok": True, "ms": round(ms, 2)}


def api_modo_escritura(d):
    EST.escritura_habilitada = bool(d.get("habilitar"))
    return {"ok": True, "habilitada": EST.escritura_habilitada}


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
        for k in ("fuente_di", "puntos", "equipos"):
            if k in d:
                EST.cfg[k] = d[k]
        guardar_config(EST.cfg)
    return {"ok": True, "config": EST.cfg, "escritura": EST.escritura_habilitada,
            "conectado": EST.equipo, "carpeta_datos": DATOS}


def api_ping(d):
    r = ping(d.get("ip", ""), int(d.get("n", 4)))
    r["puerto502"] = probar_puerto_tcp(d.get("ip", ""), d.get("puerto", 502))
    registrar("lecturas", CAB_LECTURA, [
        ahora(), "ping", d.get("ip", ""), "-", "-", "-",
        "prom=%s ms" % r.get("promedio"), r.get("promedio") or "", "diagnóstico de red",
    ])
    return {"ok": True, "resultado": r}


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
    "/api/escanear": api_escanear,
    "/api/snapshot": api_snapshot,
    "/api/comparar": api_comparar,
    "/api/di": api_di,
    "/api/escribir": api_escribir,
    "/api/modo-escritura": api_modo_escritura,
    "/api/nota": api_nota,
    "/api/signoff": api_signoff,
    "/api/config": api_config,
    "/api/ping": api_ping,
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
        except ModbusError as e:
            return self._enviar(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        except Exception as e:
            return self._enviar(json.dumps({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}, ensure_ascii=False))


# --------------------------------------------------------------------------
# Simulador de equipo Modbus TCP (para probar sin hardware)
# --------------------------------------------------------------------------

class Simulador(threading.Thread):
    """Esclavo Modbus TCP mínimo. Registro 54 (0-based) mueve bits como si fueran las DI."""

    daemon = True

    def __init__(self, puerto=5502):
        super().__init__()
        self.puerto = puerto
        self.registros = [0] * 300
        for i in range(300):
            self.registros[i] = 0
        self.registros[10] = 13800   # tensión ficticia
        self.registros[11] = 6000    # potencia ficticia
        threading.Thread(target=self._animar, daemon=True).start()

    def _animar(self):
        paso = 0
        while True:
            time.sleep(6)
            paso = (paso + 1) % 7
            self.registros[54] = 0 if paso == 0 else (1 << (paso - 1))
            self.registros[10] = 13800 + (paso * 7)

    def run(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", self.puerto))
        srv.listen(5)
        while True:
            c, _ = srv.accept()
            threading.Thread(target=self._cliente, args=(c,), daemon=True).start()

    def _cliente(self, c):
        try:
            while True:
                cab = c.recv(7)
                if len(cab) < 7:
                    return
                tid, _p, largo, uid = struct.unpack(">HHHB", cab)
                pdu = c.recv(largo - 1)
                fn = pdu[0]
                if fn in (1, 2, 3, 4):
                    dirn, cant = struct.unpack(">HH", pdu[1:5])
                    if fn in (3, 4):
                        vals = [self.registros[(dirn + i) % 300] for i in range(cant)]
                        cuerpo = bytes([fn, cant * 2]) + b"".join(struct.pack(">H", v) for v in vals)
                    else:
                        bits = [(self.registros[54] >> ((dirn + i) % 16)) & 1 for i in range(cant)]
                        nb = (cant + 7) // 8
                        crudo = bytearray(nb)
                        for i, b in enumerate(bits):
                            if b:
                                crudo[i // 8] |= 1 << (i % 8)
                        cuerpo = bytes([fn, nb]) + bytes(crudo)
                elif fn == 6:
                    dirn, val = struct.unpack(">HH", pdu[1:5])
                    self.registros[dirn % 300] = val
                    cuerpo = pdu[:5]
                elif fn == 5:
                    dirn, val = struct.unpack(">HH", pdu[1:5])
                    if val:
                        self.registros[54] |= 1 << (dirn % 16)
                    else:
                        self.registros[54] &= ~(1 << (dirn % 16))
                    cuerpo = pdu[:5]
                else:
                    cuerpo = bytes([fn | 0x80, 1])
                c.sendall(struct.pack(">HHHB", tid, 0, len(cuerpo) + 1, uid) + cuerpo)
        except Exception:
            pass
        finally:
            c.close()


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Pruebas locales — Subestación YB-6000KVA")
    ap.add_argument("--puerto", type=int, default=8080, help="puerto de la interfaz web")
    ap.add_argument("--simular", action="store_true", help="levantar equipo Modbus simulado en 127.0.0.1:5502")
    args = ap.parse_args()

    os.makedirs(DATOS, exist_ok=True)
    if args.simular:
        Simulador(5502).start()
        print("Simulador Modbus TCP activo en 127.0.0.1:5502")

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
