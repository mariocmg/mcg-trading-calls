# FLOW — qué hacen las instituciones en MU, en una frase

Una página oscura con 3 bloques, nada más. Sin modelos, sin ML, sin paneles.

## Arrancar

Doble clic en `iniciar.bat` (crea el `.venv` e instala dependencias si hace falta,
levanta el servidor y abre http://127.0.0.1:8787/). O a mano:

```
python -m flow          # servidor + escaneo cada 2 min con el mercado abierto
python -m flow scan     # una pasada y veredicto por consola, sin servidor
```

## Los 3 bloques

1. **VEREDICTO** — una línea grande: ALCISTA / BAJISTA / NEUTRAL, más una frase
   con la evidencia (el print mayor con sesgo). Se calcula sumando las primas
   con sesgo: compras de calls y ventas de puts empujan al alza; compras de puts
   y ventas de calls, a la baja. Neto ≥ +500.000 $ = ALCISTA, ≤ −500.000 $ = BAJISTA.
2. **PRINTS** — los prints inusuales del día, por prima. Un print cuenta si su
   prima (Δvolumen × precio × 100) es ≥ 50.000 $ y su volumen es inusual frente
   al interés abierto (Δvolumen ≥ 50 contratos y ≥ 0,5 × OI, o volumen del día ≥ OI).
   El sesgo compra/venta sale del último precio frente a bid/ask.
3. **NIVELES** — del vencimiento semanal: muro call (mayor OI en calls,
   resistencia), muro put (mayor OI en puts, soporte) y max pain.

## Datos y límites (léelo, es una línea honesta)

Datos **delayed** del endpoint público de Cboe (fallback Yahoo si Cboe falla),
sin API keys y sin tape real: el sesgo compra/venta es **probabilístico**
(último precio pegado al bid/ask), no una lectura de la cinta. El escaneo corre
cada 2 minutos solo con el mercado USA abierto (lun–vie 09:30–16:00 ET).

## Archivos

```
flow/fetch.py    cadena de MU: Cboe + fallback Yahoo
flow/engine.py   las 3 reglas, el veredicto y los niveles
flow/store.py    JSON planos en data/ (snapshot, prints del día, estado)
flow/server.py   web + bucle de escaneo
flow/static/     la página (un solo HTML)
tests/           8 tests de reglas, veredicto, niveles y parseo OCC
```

Versión anterior (ML, SQLite, campañas, backtests): pedida de simplificación
radical al estilo Blademap — una sola respuesta directa. La copia vieja no se
borra del historial de git: está en el commit anterior al rewrite.
