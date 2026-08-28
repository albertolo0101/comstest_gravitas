# Pruebas locales — Subestación YB-6000KVA

Herramienta de campo para comisionamiento local del NRT-333T y el Easergy P3U30.
Cliente IEC 60870-5-104 + interfaz web, **sin dependencias**: solo Python 3.8 o superior.
Nada de `pip install` en una laptop sin internet parada frente a la celda de MT.

> **Cambio de protocolo (2026-08-27):** el NRT-333T no habla Modbus TCP — el puerto 502
> rechaza la conexión, pero el 2404 (IEC 104) responde. Esta versión reemplaza el cliente
> Modbus por completo. Los comandos de mando (C_SC/C_DC) todavía **no** están implementados;
> por ahora la herramienta es de solo lectura/monitoreo.
>
> **Actualización (2026-08-27, misma tarde):** probado contra el NRT-333T real
> (172.20.251.88). CA=1 funciona, STARTDT confirma, la interrogación general devuelve
> 235 objetos y termina limpio. Mapeo de bornera X6 confirmado como esquema secuencial
> simple (**IOA = número de DI**, ver "Notas de ingeniería"). Detalle en
> `datos/pruebas_20260827.csv`.

## Arranque

```bash
python campo.py
```

Abrir `http://127.0.0.1:8080`.

Antes de ir a planta, para conocer la interfaz sin hardware:

```bash
python campo.py --simular
```

Levanta un equipo IEC 104 falso en `127.0.0.1:2404` (perfil "Simulador local" en la
pestaña Enlace/Test). Responde interrogación general con 6 puntos simples (bornera X6)
y una medida flotante, y manda una actualización espontánea cada 6 segundos — así se
ve el comportamiento real de los LED y la bitácora de transiciones.

## Antes de conectar

Tarjeta de red de la laptop en **172.20.251.100 / 255.255.0.0 / GW 172.20.251.1**.
Sin IP estática en el mismo segmento, el NRT-333T no aparece.

## Qué se confirmó del NRT-333T (2026-08-27)

- **Protocolo:** IEC 60870-5-104 estándar. El cliente de esta herramienta (framing APCI,
  STARTDT, ASDU) funciona de punta a punta contra el equipo real — descarta que fuera la
  variante propietaria "Nanzi Ethernet 103".
- **Dirección común de ASDU (CA):** `1`. Confirmado — STARTDT y la interrogación general
  responden con ese valor.
- **Interrogación general:** responde con 235 objetos y termina limpio (ASDU de fin de
  activación, COT=10). No exige sincronización de hora previa.
- **Bloque de telesignales (SP/DP):** IOA 1 a ~192, todos `M_SP_NA_1` (punto simple).
- **Bloque de telemetría analógica:** empieza en IOA 16385 (`0x4001`), 43 valores
  `M_ME_NC_1` (flotante IEEE-754). Escala/significado de cada canal: sin confirmar.
- **Mapeo de bornera X6:** esquema secuencial simple, **IOA = número de DI** (DI 1 →
  IOA 1 … DI 6 → IOA 6). Confirmado para DI 3 por correlación con la luz de Alta
  Temperatura encendida en el panel local, estable en dos interrogaciones separadas.
  DI 1, 2, 4, 5 y 6 están **inferidos** del mismo bloque contiguo, no confirmados
  individualmente — para eso sirve el Comparador: puentee un contacto a la vez y vea
  qué IOA cambia.

Sigue pendiente: qué son los otros ~186 puntos simples del bloque 1-192 (18 están en 1
ahora mismo — probablemente banderas de estado normal, no alarmas, pero sin confirmar) y
el significado/escala de los 43 canales analógicos. La pestaña **Explorador** vuelca todo
eso tal cual llega; cruce contra el manual o contra acciones físicas conocidas para irlos
identificando.

## Las siete pestañas

| Pestaña | Para qué |
|---|---|
| **Test** | Un click corre todo lo automatizable contra el NRT-333T: ping, puerto 2404, enlace IEC 104 (STARTDT) e interrogación general contra la bornera X6 |
| **Enlace** | Conectar, ping ICMP, verificación del puerto 2404 y lectura puntual de un IOA (C_RD_NA_1) |
| **Alarmas X6** | Monitoreo continuo de las 6 DI del transformador, con LED por punto y estampado de hora en cada transición |
| **Explorador** | Interrogación general completa — vuelca todo lo que el equipo reporte, para descubrir el mapeo real |
| **Comparador** | Captura antes/después (dos interrogaciones generales) para mapear señales no documentadas |
| **Sign-off** | Las 4 pruebas del acta del manual, exportables a CSV |
| **Datos** | Bitácora manual y descarga de archivos. Comandos de mando: pendientes |

## Todo queda en `datos/`

| Archivo | Contenido |
|---|---|
| `pruebas_AAAAMMDD.csv` | Resultado de cada corrida de la pestaña Test: OK/FALLA por paso |
| `lecturas_AAAAMMDD.csv` | Cada lectura puntual con hora, IOA, tipo, valor y latencia |
| `eventos_AAAAMMDD.csv` | Cada transición 0→1 y 1→0 de las DI, con borne e instrumento |
| `hallazgos_AAAAMMDD.csv` | Resultados del comparador |
| `bitacora_AAAAMMDD.csv` | Notas manuales |
| `signoff_AAAAMMDD_HHMM.csv` | Acta de aceptación |

El `eventos_*.csv` es la evidencia útil: hora exacta de cada actuación en bornera
contra el IOA que respondió.

## Notas de ingeniería

**Los IOA 1-6 en `config.json` son el esquema secuencial simple, confirmado solo
parcialmente.** DI 3 está corroborado contra una condición física real (luz de Alta
Temperatura). DI 1, 2, 4, 5 y 6 se infirieron por continuidad del mismo bloque — muy
probable que sean correctos, pero no dé por buena una lectura de la bornera X6 sin
confirmar cada uno con el Comparador (puente físico contacto por contacto) cuando tenga
acceso a bornera.

**El monitoreo de Alarmas X6 no vuelve a tocar la red en cada tick.** Al conectar/leer
una vez se manda una interrogación general que siembra una caché por IOA; un hilo de
lectura en segundo plano sigue escuchando el socket y actualiza esa caché con cualquier
actualización espontánea (COT=3) que llegue sola, sin que la interfaz tenga que pedirla.
El "Iniciar monitoreo" solo refresca la vista desde esa caché — es deliberado, no un
recorte: así es como IEC 104 está pensado para usarse.

**El comparador es para lo que no está documentado.** Todo en reposo → captura A →
accione una sola señal → captura B → comparar. Lo que cambió es su punto. Sirve para
telemetría analógica del transformador que no aparece en el manual.

**La prueba 4 no va por aquí.** El disparo forzado del P3U30 es por USB A-B con eSetup
Easergy Pro (Operator, clave 0001 → Relays → Forcing Flag → T1 en X3:16-17). No hay
forma de hacerlo con esta herramienta y no debería haberla.

**El P3U30 en 172.20.251.90 está sin confirmar** — ni la IP ni el protocolo. El Easergy
P3U30 suele soportar varios protocolos (IEC 104, IEC 61850, DNP3, Modbus según modelo);
no asuma que también es IEC 104 solo porque el NRT-333T lo es.

**Nunca deje los cables de comunicación conectados** durante pruebas de rigidez
dieléctrica o inyección secundaria.

## Estructura

```
campo.py      motor: cliente IEC 104, servidor local, simulador, registro CSV
ui.html       interfaz
config.json   equipos, mapeo de puntos y acta (se crea solo en el primer arranque)
datos/        salidas CSV
```

`config.json` se edita a mano cuando el mapeo real difiera de lo asumido: cambie `ca`
en cada equipo y el `ioa` de cada punto en `puntos`.

## Pendientes para el repo

- Confirmar DI 1, 2, 4, 5 y 6 uno por uno con el Comparador (solo DI 3 está corroborado)
- Identificar los ~186 puntos simples restantes del bloque IOA 1-192 (18 están en 1 ahora)
- Identificar y escalar los 43 canales analógicos del bloque IOA 16385+
- Comandos de mando (C_SC_NA_1/C_DC_NA_1) — pendiente hasta confirmar los IOA de mando
- P3U30: confirmar si tiene puerto Ethernet o es solo RS-485 (ver nota más abajo)
- Exportar el mapeo confirmado como plantilla de puntos para el SCADA central
- Multímetro GGD — protocolo por confirmar (antes se asumía Modbus RTU)
- Sellado del acta en PDF con las capturas del comparador adjuntas
