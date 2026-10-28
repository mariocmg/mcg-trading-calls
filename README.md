# FLOW — qué hacen las instituciones en MU, en una frase

Una página oscura con el diseño clásico (hero, book, niveles, targets,
campañas, tape), alimentada por un detector explicable. Sin modelos
black-box: cada punto de convicción tiene un motivo visible en el código
y en el modal "¿Cómo se calcula?".

## Arrancar

Doble clic en `iniciar.bat` (crea el `.venv` e instala dependencias si hace falta,
levanta el servidor y abre http://127.0.0.1:8787/). O a mano:

```
python -m flow          # servidor + escaneo cada 30 s con el mercado abierto
python -m flow scan     # una pasada y veredicto por consola, sin servidor
```

La página pregunta cada 10 s (botón LIVE pausa el auto-refresh); Actualizar
lanza un scan manual.

## El detector (sin ML, a propósito)

La versión anterior puntuaba con un GradientBoosting cuyas etiquetas de
outcome eran en parte sintéticas y cuyo razonamiento no se podía auditar.
Este detector es reglas visibles:

1. **Print**: prima (Δvolumen × precio × 100) ≥ 50.000 $ y Δvolumen ≥ 50
   contratos y ≥ 0,5 × OI (o volumen del día ≥ OI).
2. **ΔOI y posición nueva**: el snapshot guarda también el OI previo. ΔOI > 0
   = apertura institucional; volumen alto con OI plano o cayendo = cierre o
   churn, no posicionamiento. (El OI intradía de las cadenas delayed suele
   ser el del cierre anterior: el ΔOI del primer scan del día mide el cambio
   entre sesiones.)
3. **Barridos**: en un mismo scan, ≥ 3 prints con el mismo sesgo, call/put y
   vencimiento, en strikes dentro de un 10% del spot = una sola mano agresiva.
4. **Convicción 0–100**: base 20 + prima (log, hasta +30) + vol/OI (hasta +20)
   + posición nueva +15 (cierre neto −10) + sesgo claro +10 + barrido +15.
5. **Veredicto ponderado**: cada prima con sesgo se multiplica por su
   convicción/100. Compras de calls y ventas de puts = alcista; compras de
   puts y ventas de calls = bajista. Neto ≥ +500.000 $ = ALCISTA,
   ≤ −500.000 $ = BAJISTA, si no NEUTRAL. Sin sesgo claro no se vota.
6. **Confianza y etapa**: confianza desde acuerdo de dirección, magnitud del
   neto y nº de prints con sesgo (un NEUTRAL se divide entre 2); etapa
   Temprano/Construyendo/Confirmado según confianza y persistencia del
   veredicto entre scans.
7. **Niveles**: muros de OI del semanal (call = resistencia, put = soporte),
   max pain, y el muro opuesto como invalidación. Sin gamma flip ni régimen
   gamma: la cadena no trae IV ni griegas y no se inventan.

## Datos y límites (léelo, es una línea honesta)

Datos **delayed** del endpoint público de Cboe (fallback Yahoo si Cboe falla),
sin API keys y sin tape real: el sesgo compra/venta es **probabilístico**
(último precio pegado al bid/ask), no una lectura de la cinta. El escaneo
corre cada 30 segundos solo con el mercado USA abierto (lun–vie 09:30–16:00
ET). El refresco rápido mantiene la página al día, pero **no es cinta en
vivo**: un feed tick a tick real sería de pago y no está incluido.

## Archivos

```
flow/fetch.py    cadena de MU: Cboe + fallback Yahoo
flow/engine.py   reglas, ΔOI, barridos, convicción, veredicto ponderado,
                 book, targets, campañas, niveles
flow/store.py    JSON planos en data/ (snapshot con volumen y OI, prints
                 del día, estado)
flow/server.py   web + bucle de escaneo cada 30 s
flow/static/     la página (HTML + CSS del diseño clásico)
tests/           tests de reglas, ΔOI, barridos, convicción, veredicto
                 ponderado, campañas, book y parseo OCC
```

Versión anterior (ML, SQLite, campañas, backtests): pedida de simplificación
radical al estilo Blademap — una sola respuesta directa. La copia vieja no se
borra del historial de git: está en el commit anterior al rewrite.
