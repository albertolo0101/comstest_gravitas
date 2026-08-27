# Pruebas locales — Subestación YB-6000KVA

Herramienta de campo para comisionamiento local del NRT-333T y el Easergy P3U30.
Cliente Modbus TCP + interfaz web, **sin dependencias**: solo Python 3.8 o superior.
Nada de `pip install` en una laptop sin internet parada frente a la celda de MT.

## Arranque

```bash
python campo.py
```

Abrir `http://127.0.0.1:8080`.

Antes de ir a planta, para conocer la interfaz sin hardware:

```bash
python campo.py --simular
```

Levanta un equipo Modbus falso en `127.0.0.1:5502` (perfil "Simulador local" en la
pestaña Enlace). Mueve bits del registro 55 cada 6 segundos, así se ve el
comportamiento real de los LED y la bitácora de transiciones.

## Antes de conectar

Tarjeta de red de la laptop en **172.20.251.100 / 255.255.0.0 / GW 172.20.251.1**.
Sin IP estática en el mismo segmento, el NRT-333T no aparece.

## Las seis pestañas

| Pestaña | Para qué |
|---|---|
| **Enlace** | Conectar, ping ICMP, verificación del puerto 502 y lectura puntual de cualquier registro |
| **Alarmas X6** | Monitoreo continuo de las 6 DI del transformador, con LED por punto y estampado de hora en cada transición |
| **Explorador** | Barrido de un rango de registros para encontrar dónde vive realmente el dato |
| **Comparador** | Captura antes/después para mapear señales no documentadas |
| **Sign-off** | Las 4 pruebas del acta del manual, exportables a CSV |
| **Datos** | Bitácora manual, descarga de archivos y modo escritura (bloqueado por defecto) |

## Todo queda en `datos/`

| Archivo | Contenido |
|---|---|
| `lecturas_AAAAMMDD.csv` | Cada lectura con hora, offset, valores y latencia |
| `eventos_AAAAMMDD.csv` | Cada transición 0→1 y 1→0 de las DI, con borne e instrumento |
| `hallazgos_AAAAMMDD.csv` | Resultados del comparador |
| `escrituras_AAAAMMDD.csv` | Auditoría de todo lo escrito al equipo |
| `bitacora_AAAAMMDD.csv` | Notas manuales |
| `signoff_AAAAMMDD_HHMM.csv` | Acta de aceptación |

El `eventos_*.csv` es la evidencia útil: hora exacta de cada actuación en bornera
contra el bit que respondió.

## Notas de ingeniería

**El registro 55 puede no ser el 55.** El manual dice "registro de retención decimal 55",
pero no aclara si es offset 0-based o número 1-based, y en la misma tabla lo llama
"registro DI" — lo que también podría implicar función 02 en lugar de 03. Por eso la
herramienta permite alternar función y numeración, y por eso existe el Explorador:
si el 55 sale vacío, barra 0–120 en FC 03 y FC 04 y busque un registro cuyos bits
bajos se muevan al puentear la bornera. Cuando lo confirme, guárdelo en `config.json`.

**El comparador es para lo que no está documentado.** Todo en reposo → captura A →
accione una sola señal → captura B → comparar. Lo que cambió es su punto. Sirve para
telemetría analógica del transformador que no aparece en el manual.

**La prueba 4 no va por aquí.** El disparo forzado del P3U30 es por USB A-B con eSetup
Easergy Pro (Operator, clave 0001 → Relays → Forcing Flag → T1 en X3:16-17). No hay
forma de hacerlo por Modbus con esta herramienta y no debería haberla.

**El P3U30 en 172.20.251.90 está sin confirmar.** El manual la marca como sugerida.
Si no responde a ping, probablemente hay que asignarla primero por USB.

**Nunca deje los cables de comunicación conectados** durante pruebas de rigidez
dieléctrica o inyección secundaria.

## Estructura

```
campo.py      motor: cliente Modbus, servidor local, simulador, registro CSV
ui.html       interfaz
config.json   equipos, mapeo de puntos y acta (se crea solo en el primer arranque)
datos/        salidas CSV
```

`config.json` se edita a mano si el mapeo real difiere del manual: cambie `fuente_di`
(fc, dirección, numeración) y los `puntos` (bit, borne, nombre).

## Pendientes para el repo

- Exportar el mapeo confirmado como plantilla de puntos para el SCADA central
- Multímetro GGD por Modbus RTU (RS485, 9600 n.8.1) — requiere `pyserial`
- Lectura de telemetría analógica del NRT-333T con escalamiento
- Sellado del acta en PDF con las capturas del comparador adjuntas
