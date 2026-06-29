# Proyecto integral

El repositorio ahora incluye un ejemplo separado en:

```text
examples/full-project/
```

## Que cubre

El proyecto demuestra en una sola base:

- `App`, rutas `view`, `api` y `ws`.
- documentacion runtime con `enable_docs()`.
- metricas, logs NDJSON y tareas en background.
- ORM SQLite con modelos persistentes.
- cache clave-valor y cache HTTP de vistas.
- `SecurityPolicy`.
- `ProxyI` con upstream separado.
- `LocalDNSServer` opcional.
- `Predictor` y `NeuralNetwork` para una capa simple de IA.

## Archivos principales

```text
examples/full-project/
├── README.md
├── pyproject.toml
├── app.py
└── upstream.py
```

## Como correrlo

1. Instala `wsbuilder` desde este repo o desde la wheel local.
2. Levanta el upstream:

   ```bash
   python upstream.py --port 8780
   ```

3. En otra terminal, levanta la app principal:

   ```bash
   python app.py --port 8765 --upstream-port 8780
   ```

4. Abre:

   - `/`
   - `/docs`
   - `/api/metrics`
   - `/proxy`
   - `/api/notes`
   - `/ws/echo`

## DNS opcional

El ejemplo no inicia el servidor DNS por defecto para no ocupar puertos ni
introducir ruido en demos simples. Para activarlo:

```bash
WSB_FULL_DEMO_ENABLE_DNS=1 python app.py
```

El ejemplo usa `127.0.0.1:5533` como valor por defecto para evitar privilegios
de root.
