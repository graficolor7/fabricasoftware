# Supervisor automático (Claude ↔ ChatGPT)

Claude/Opus construye; un supervisor independiente (hoy ChatGPT vía API de OpenAI) audita **solo en quality gates significativos**; las herramientas deterministas verifican; el Product Owner (PO) decide.
El objetivo no es que el supervisor diga PASS: es producir software correcto.

Contenido: [Instalación](#instalación) · [Configuración](#configuración) · [Flujo](#flujo-del-supervisor) · [Revisión automática](#revisión-automática) · [Revisión manual](#revisión-manual) · [GitHub](#github) · [Secretos](#secretos) · [Control de costos](#control-de-costos) · [Recuperación de fallos](#recuperación-de-fallos) · [Nuevo proyecto](#añadir-un-proyecto-nuevo) · [Cambiar de modelo](#cambiar-el-modelo-o-proveedor) · [Override del PO](#override-del-product-owner) · [Reconstruir una revisión](#reconstruir-una-revisión)

## Instalación
```
git clone https://github.com/graficolor7/fabricasoftware .fabrica   # o en cualquier ruta
pip install -r .fabrica/requirements.txt                            # jsonschema + pytest; sin SDKs
export PYTHONPATH=$PWD/.fabrica                                      # para `python -m factory`
python -m factory --project-root RUTA_DEL_PROYECTO status
```
Python ≥ 3.10. Sin dependencias de red salvo la llamada al proveedor. Pruebas: `python -m pytest -q` en la fábrica (sin red).

## Configuración
Una sola fuente: `config/supervisor.defaults.json` → `config` del perfil del proyecto (`.factory/supervisor.json`) → variables de entorno (ganan).
`python -m factory config` muestra los valores efectivos y de dónde salen (sin secretos).

| Variable | Defecto | Qué hace |
|---|---|---|
| `SUPERVISOR_PROVIDER` | `mock` | `openai` · `mock` · `manual` |
| `SUPERVISOR_LIVE_ENABLED` | `false` | interruptor de llamadas reales. Solo el PO lo pone en `true` |
| `SUPERVISOR_MODEL` | vacío | modelo del supervisor (se elige en la documentación oficial de OpenAI). `OPENAI_MODEL` se acepta por compatibilidad |
| `SUPERVISOR_REASONING_LEVEL` | vacío | `reasoning.effort` si el modelo lo admite (los valores dependen del modelo) |
| `SUPERVISOR_MAX_OUTPUT` | 25000 | `max_output_tokens` (incluye razonamiento) |
| `SUPERVISOR_TIMEOUT` | 300 | segundos por llamada HTTP |
| `SUPERVISOR_MAX_RETRIES` / `SUPERVISOR_BACKOFF_BASE_S` / `SUPERVISOR_BACKOFF_MAX_S` | 3 / 2 / 30 | reintentos con backoff exponencial (respeta `Retry-After`) |
| `SUPERVISOR_MAX_REPAIR_ATTEMPTS` | 1 | reparaciones si la respuesta no cumple el schema |
| `SUPERVISOR_MAX_EVIDENCE_ROUNDS` | 2 | rondas NEED_EVIDENCE seguidas |
| `SUPERVISOR_MAX_DELTA_ROUNDS` | 3 | re-revisiones tras correcciones |
| `SUPERVISOR_MAX_CALLS_PER_GATE` | 8 | llamadas (incluye reparaciones) por revisión |
| `SUPERVISOR_MAX_CONTEXT_BYTES` | 120000 | tamaño máximo del paquete |
| `SUPERVISOR_MAX_FILES_PER_REQUEST` / `SUPERVISOR_MAX_EVIDENCE_BYTES_PER_FILE` | 8 / 20000 | evidencia por ronda |
| `SUPERVISOR_MAX_DISAGREEMENT_ROUNDS` | 2 | disputas del mismo hallazgo antes de escalar |
| `SUPERVISOR_BLOCKING_SEVERITIES` | `CRITICAL,HIGH` | severidades que siempre bloquean (CRITICAL siempre) |
| `SUPERVISOR_ON_UNAVAILABLE` | `SUPERVISOR_PENDING` | o `HUMAN_REVIEW_REQUIRED` |
| `SUPERVISOR_REQUIRE_GREEN_VERIFICATION` | `true` | con `--run-verification`, no se gasta si CI local falla |
| `SUPERVISOR_ALLOW_MOCK_CLOSURE` | `false` | un veredicto del Mock nunca cierra un gate real |
| `SUPERVISOR_PRICE_*_PER_1M` | vacío | tarifas oficiales; sin ellas no se calcula costo |

## Flujo del supervisor
```
Claude/Opus → trabajo delegado → implementación → tests/verificación → commit
   → QUALITY GATE → trigger válido → Supervisor Packet (compacto, validado, hash)
   → SupervisorAdapter → proveedor → veredicto JSON estricto → política de la fábrica
   → PASS | PASS_WITH_ACTIONS | BLOCKED | NEED_EVIDENCE | HUMAN_REVIEW_REQUIRED
   → registro persistido → estado actualizado → acción de Claude → (delta) → cierre del gate
```
**Cuándo:** solo con un trigger del gate: `MILESTONE_COMPLETION`, `ARCHITECTURE_FREEZE`, `ARCHITECTURE_MAJOR_CHANGE`, `SHARED_CONTRACT_FREEZE`, `SHARED_CONTRACT_BREAKING_CHANGE`, `SECURITY_CRITICAL_CHANGE`, `CRITICAL_BUG_UNRESOLVED`, `DOCUMENT_MODEL_FREEZE`, `FILE_FORMAT_FREEZE`, `RENDERING_ARCHITECTURE_FREEZE`, `PRE_RELEASE`, `MANUAL_SUPERVISOR_REQUEST`, `BASELINE_SUPERVISOR_REVIEW` + los del perfil. Formato, typos, refactors pequeños, tests rutinarios, ajustes menores de UI y commits rutinarios **no** llaman al supervisor (lo cubre CI), salvo que las reglas de escalado del perfil lo pidan.

**Qué hace `factory supervisor review`:** identifica proyecto (perfil) y gate · valida el trigger · corre la verificación determinista si se pide · completa el paquete con repo/rama/commit · valida schema, secretos, tamaño y marca texto sospechoso de inyección · comprueba idempotencia · toma el lock del gate · llama al adaptador · valida el veredicto · aplica la política · persiste · actualiza el estado · devuelve un resumen y un código de salida.

**Protocolo de dos pasos:** si el supervisor responde `NEED_EVIDENCE` con `evidence_requests`, la herramienta recoge **solo** eso del commit revisado (extractos con rango de líneas, tests, ADRs, contratos, diffs, commits) y hace la segunda llamada. Lo que no se puede recolectar solo (un CI run, un benchmark de la PC) se aporta con `factory supervisor evidence --review ID --evidence-file e.json`. Máximo 2 rondas; después `HUMAN_REVIEW_REQUIRED`.

**Semántica:**
| Veredicto efectivo | Efecto | Claude |
|---|---|---|
| PASS | `CAN_CLOSE` | `factory supervisor close` |
| PASS_WITH_ACTIONS sin acciones bloqueantes | `CAN_CONTINUE_WITH_PENDING_ACTIONS` | cierra y convierte las acciones en tareas |
| PASS_WITH_ACTIONS con acciones bloqueantes | `CANNOT_CLOSE` | igual que BLOCKED |
| BLOCKED | `CANNOT_CLOSE` | findings → tareas de remediación (modelo adecuado) → implementar → verificar → commit → `delta` |
| NEED_EVIDENCE | `AWAITING_EVIDENCE` | solo la evidencia pedida |
| HUMAN_REVIEW_REQUIRED | `HUMAN_REVIEW` | para y avisa al PO |

Reglas deterministas: CRITICAL y HIGH bloquean; LOW/INFO nunca bloquean; PASS con acciones → PASS_WITH_ACTIONS; PASS con evidencia pendiente → NEED_EVIDENCE; commit y gate del veredicto se fuerzan a los del paquete.

**Delta:** `factory supervisor delta --review ID --draft delta.json` envía solo `previous_review_id`, `previous_commit`, `new_commit`, `findings_addressed`, `changed_files` (git), `new_test_results`, `new_evidence`, `remaining_risks` y los hallazgos abiertos. Plantilla: `templates/SUPERVISOR_DELTA_DRAFT.json`.

**Desacuerdo:** si Claude disputa un hallazgo (`delta.disputed_findings`) y el supervisor lo mantiene bloqueante 2 veces, se genera `disagreement.json/.md` (TECHNICAL_DISAGREEMENT_REPORT: issue, posición de Opus, posición del supervisor, evidencia, tradeoffs, riesgo, resoluciones, decisión recomendada) y `HUMAN_REVIEW_REQUIRED`. No se sigue discutiendo.

## Revisión automática
Claude/Opus la ejecuta en el gate, sin pedir permiso, cuando el perfil o un trigger lo exige:
```
python -m factory --project-root . supervisor review --draft .factory/packets/M1-G6.json \
    --milestone M1 --gate G6 --trigger MILESTONE_COMPLETION --run-verification
python -m factory --project-root . status
```
Después de corregir: `supervisor delta`. Tras un fallo del proveedor: `supervisor retry`. Al terminar: `supervisor close`. Antes de un gate, `factory trigger evaluate --base origin/main` dice qué triggers activa el cambio.

## Revisión manual
`SUPERVISOR_PROVIDER=manual`: la herramienta escribe `manual-request-NN-*.md` en la carpeta de la revisión (instrucciones + paquete). El PO lo pega en ChatGPT, guarda el JSON en `manual-response-NN-*.json` (el número que indique el mensaje) y ejecuta `supervisor retry --review ID`. Se valida igual que una respuesta de la API.

## GitHub
- **Nunca en cada push.** `supervisor-gate.yml` es `workflow_call`; el proyecto lo llama desde `workflow_dispatch` (gate manual o comando de gate) o desde un workflow controlado (por ejemplo, etiqueta `supervisor-gate` puesta por el PO en un PR del mismo repo, o un release candidate).
- **Permisos:** `contents: read`, `pull-requests: write` (solo para comentar). Ninguna escritura al repo.
- **Secrets:** `OPENAI_API_KEY` (opcional para mock/manual). **Variables:** `SUPERVISOR_PROVIDER` (si falta, `openai`), `SUPERVISOR_MODEL`, `SUPERVISOR_LIVE_ENABLED`, `SUPERVISOR_REASONING_LEVEL`, límites.
- **Concurrencia:** `concurrency` por repo+milestone+gate sin cancelar; además el lock de archivo y la idempotencia.
- **Fallo:** el job termina rojo si no hay veredicto que permita cerrar (BLOCKED, inválido, pendiente, evidencia, humano, lock). Un fallo del proveedor nunca da verde.
- **Registro:** se sube como artefacto `supervisor-review` (result, resumen, `.factory/reviews/`, estado). Para conservarlo en el repo, Claude lo descarga y lo commitea en la rama del gate (las Actions no escriben al repo).
- **Costo:** cada ejecución en modo `openai` con `SUPERVISOR_LIVE_ENABLED=true` consume tokens (≤ `SUPERVISOR_MAX_CALLS_PER_GATE` llamadas). Etiquetas en PRs de forks no reciben secrets (seguro por diseño).
- `supervisor-trigger-check.yml`: aviso gratuito en cada PR de qué triggers activa el cambio. No llama al supervisor.

## Secretos
- `OPENAI_API_KEY` solo por entorno o GitHub Secret. `.env.example` tiene el placeholder vacío; `.env` está en `.gitignore`.
- El adaptador la lee en el momento de la llamada; nunca se guarda en config, paquetes, registros, logs ni se pasa a otros agentes.
- Todo lo que se escribe pasa por `redact()` (patrones de claves y valor literal de las variables secretas).
- Paquetes y evidencia se escanean antes de enviar; un paquete con algo con forma de secreto no sale.
- La configuración rechaza claves con KEY/TOKEN/SECRET/PASSWORD en el nombre.

## Control de costos
Trigger obligatorio · verificación verde antes de gastar · paquete compacto con tope de bytes · evidencia bajo demanda con topes · idempotencia (mismo proyecto+commit+gate+hash → `DUPLICATE`, sin llamada) · lock por gate · máximo de llamadas, rondas de evidencia, deltas y disputas → `HUMAN_REVIEW_REQUIRED` · `store: false` · `factory supervisor validate` estima bytes/tokens sin llamar. El uso real (`input_tokens`, `cached_tokens`, `output_tokens`, `reasoning_tokens`, modelo, duración, llamadas, rondas) queda en `calls.jsonl` y `review.json`. El costo solo se calcula si el PO configura las tarifas oficiales.

## Recuperación de fallos
| Estado | Causa | Qué hacer |
|---|---|---|
| `SUPERVISOR_PENDING` | red, 5xx, 429, timeout, clave/modelo, live deshabilitado, revisión manual pendiente | arreglar la causa → `supervisor retry --review ID` |
| `INVALID_SUPERVISOR_RESPONSE` | JSON inválido tras reparación, refusal, `incomplete` (subir `SUPERVISOR_MAX_OUTPUT`) | `retry`; si se repite, revisión humana |
| `HUMAN_REVIEW_REQUIRED` | límites, evidencia insuficiente, desacuerdo, decisión de producto | el PO decide (override, CR, nuevo plan) |
| `LOCKED` | otra revisión del mismo gate en curso | esperar; locks huérfanos expiran tras `SUPERVISOR_LOCK_TTL_S` |
| `INVALID_PACKET` | schema, secreto o tamaño | corregir el borrador; `supervisor validate` |
| `VERIFICATION_FAILED` | CI local rojo | corregir antes de gastar |
Nunca se marca PASS sin un veredicto válido persistido; el estado del proyecto solo cambia después de persistir.

## Añadir un proyecto nuevo
1. Copia `templates/SUPERVISOR_PROFILE.json` a `.factory/supervisor.json` del proyecto: `project_id`, `repository`, `review_id_prefix`, triggers adicionales, reglas de escalado por rutas, comandos de verificación, `adr_files`, `contract_files`, `allowed_roots`.
2. Añade `.github/workflows/supervisor-gate.yml` (llamador) y opcionalmente el trigger-check.
3. Primera vez en un proyecto existente: `supervisor baseline` (BASELINE_SUPERVISOR_REVIEW) con el estado actual. Después, solo deltas por gate.
4. Valida: `python tools/validate.py supervisor-profile .factory/supervisor.json`.

## Cambiar el modelo o proveedor
Solo configuración: `SUPERVISOR_MODEL=<modelo>` (y `SUPERVISOR_REASONING_LEVEL` si aplica) como variable del repo o del entorno. Otro proveedor = un nuevo adaptador que implemente `review`, `request_additional_evidence`, `review_delta`, `health_check` (`factory/adapters/base.py`) y su nombre en `SUPERVISOR_PROVIDER`. `supervisor health` comprueba clave y modelo sin gastar tokens. El prompt está versionado en `prompts/supervisor/SUPERVISOR_v2.md` (`SUPERVISOR_PROMPT`).

## Override del Product Owner
```
python -m factory supervisor override --review ID --finding SUP-003 --reason "…" \
    --authorized-by "Joseph (PO)" --risk-accepted "…" --i-am-the-product-owner
```
Solo con instrucción escrita del PO; Claude nunca lo ejecuta por su cuenta. Registra `OVERRIDE_ID`, hallazgo completo, motivo, quién, fecha y riesgo aceptado en `overrides.jsonl`; el hallazgo sigue en el veredicto con `overridden_by` y el gate pasa a `PASS_WITH_ACTIONS` con riesgo aceptado.

## Reconstruir una revisión
`.factory/reviews/<proyecto>/<MILESTONE>/<REVIEW_ID>/`: `packet.json` (qué pidió Claude), `evidence-*-packet.json` y `evidence-index.json` (qué evidencia envió, con sha256), `round-NN.json` (commit, hash, modelo, veredicto crudo y efectivo, llamadas), `delta-*-packet.json` (qué se corrigió), `verdict.json`, `review.json` (`closed_by`: qué revisión y ronda cerró el gate), `calls.jsonl`, `disagreement.*`. `index.json` guarda IDs y claves de idempotencia; `overrides.jsonl` las decisiones del PO.

## Ética del paquete
Nunca ocultar fallos, reducir evidencia para engañar al supervisor, cambiar tests para que pasen sin corregir el problema ni reformular el paquete para esconder riesgos. `known_failures` y `risks` se declaran siempre.
