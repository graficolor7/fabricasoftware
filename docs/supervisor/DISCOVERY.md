# Discovery · Supervisor automático (Claude ↔ ChatGPT)

Fecha: 2026-11-03 · Base: `fabricasoftware` rama `factory/v1` (PR #1, fábrica 1.0.0, sin fusionar) · Proyecto piloto: `GrafiArtsComplete` (`main` = `31ec4da`).

## A. Infraestructura existente
| Pieza | Dónde | Estado |
|---|---|---|
| Constitución, roles, gates G0–G7 | `FACTORY.md`, `policies/TAREAS_Y_GATES.md` | sirve tal cual; G6 = gate del supervisor |
| Política del supervisor | `policies/SUPERVISOR.md` | cuándo se consulta, semántica PASS/PASS_WITH_ACTIONS/BLOCKED, anti-bucle (3 rondas, desacuerdo ×2), operación degradada |
| Schemas | `schemas/supervisor-packet.schema.json` (v1, plano), `supervisor-verdict.schema.json` (v1, estricto OpenAI), `project-state`, `task`, `change-request` | v1 sin NEED_EVIDENCE, sin INFO, sin repo/branch, sin delta, sin `schema_version` en el veredicto |
| Adaptador | `adapters/supervisor/supervisor.py` | un solo disparo: valida paquete, busca secretos, llama a la API Responses con `json_schema` estricto, 1 reintento si el JSON es inválido, reglas deterministas, códigos de salida |
| Prompt del supervisor | `prompts/CHATGPT_SUPERVISOR.md` | sin versión; no trata el contenido del proyecto como datos no confiables |
| Workflow | `.github/workflows/supervisor-gate.yml` (reutilizable, `workflow_call`) | sin `concurrency`, sin persistencia del registro, depende de `OPENAI_MODEL` |
| CI | `.github/workflows/factory-ci.yml` | schemas + plantillas + pytest (23 tests verdes) |
| Plantillas | `templates/DISAGREEMENT_REPORT.md`, `SUPERVISOR_PACKET.json`, `MILESTONE_CLOSURE.md` … | se reutilizan |
| Proyecto | GrafiArts PR #1 (`factory/f1-bootstrap`): `.factory/PROFILE.md`, `PROJECT_STATE.json`, `packets/`, `verdicts/`, `supervisor-gate.yml` (workflow_dispatch) | sin fusionar |

## B. Reutilizable
Validación de paquete y búsqueda de secretos, construcción de la petición Responses + schema estricto, extracción de texto y detección de `refusal`, `apply_policy`, códigos de salida, workflow reutilizable, plantilla de desacuerdo, perfil y estado de GrafiArts, gates G0–G7 y nomenclatura del proyecto.

## C. Falta
Abstracción `SupervisorAdapter` (Mock, Manual); protocolo de 2 pasos con NEED_EVIDENCE y recolección segura de evidencia; delta review; registro `.factory/reviews/…` reproducible; identidad, idempotencia y lock; límites de coste; reintentos con backoff y SUPERVISOR_UNAVAILABLE; INVALID_SUPERVISOR_RESPONSE; desacuerdo automático; overrides del PO; `factory supervisor review` y `factory status`; triggers universales + por perfil y reglas de escalado por rutas; telemetría de uso; prompt versionado con defensa de inyección; documentación operativa.

## D. Arquitectura del SupervisorAdapter
```
factory/                       paquete Python (stdlib + jsonschema)
  config.py                    config central: defaults → perfil del proyecto → variables de entorno
  adapters/base.py             SupervisorAdapter: review · request_additional_evidence · review_delta · health_check
  adapters/openai_adapter.py   OpenAISupervisorAdapter (API Responses, json_schema estricto, store=false)
  adapters/mock.py             MockSupervisorAdapter (escenarios y guiones)
  adapters/manual.py           ManualSupervisorAdapter (petición a archivo, veredicto pegado por un humano)
  packet.py · evidence.py      paquete compacto, hash, delta; evidencia acotada y segura
  policy.py · triggers.py      reglas deterministas del veredicto; triggers y escalado por rutas
  store.py                     registro, IDs, índice de idempotencia, locks, escritura atómica
  review.py                    orquestador (rondas, límites, desacuerdo, estado)
  status.py · overrides.py     `factory status`; overrides del PO
  cli.py                       python -m factory …
```
El adaptador solo transporta y valida; las decisiones (bloquear, rondas, límites) están en `policy.py`/`review.py`, comunes a todos los proveedores.

## E. Flujo exacto
1. `python -m factory supervisor review --project-root R --gate G --trigger T --draft D` identifica proyecto (perfil) y gate.
2. Comprueba que T es un trigger válido (universal o del perfil). Si no, termina sin gastar.
3. Opcional `--run-verification`: corre los comandos del perfil (sin shell). Si fallan y el perfil lo exige → `VERIFICATION_FAILED`, sin llamada.
4. Completa el paquete (repo, rama, commit, PR, versión de fábrica), valida schema, secretos, tamaño y marca texto sospechoso de inyección.
5. Calcula `packet_hash`; si ya existe una revisión válida para proyecto+commit+gate+hash → `DUPLICATE` (devuelve la anterior).
6. Toma el lock del gate; asigna `REVIEW_ID` (`GRAFIARTS-M1-G6-0001`).
7. Llama al adaptador. Fallo de red/5xx/429/timeout → reintentos con backoff → `SUPERVISOR_UNAVAILABLE` → `SUPERVISOR_PENDING` (o `HUMAN_REVIEW_REQUIRED` según política).
8. Valida el veredicto; si no cumple, 1 reparación; después `INVALID_SUPERVISOR_RESPONSE`.
9. Aplica política; persiste `packet.json`, `verdict-*.json`, `calls.jsonl`, `evidence-index.json`.
10. Solo entonces actualiza `.factory/SUPERVISOR_STATE.json`.
11. `NEED_EVIDENCE` → recoge **solo** lo pedido (máx. N archivos, M bytes) y repite; máximo 2 rondas; luego `HUMAN_REVIEW_REQUIRED`.
12. `BLOCKED` → tareas de remediación → commit → `factory supervisor delta` con solo lo cambiado. Si el mismo hallazgo se disputa 2 veces → `TECHNICAL_DISAGREEMENT_REPORT` + `HUMAN_REVIEW_REQUIRED`.
13. Devuelve un resumen corto y un código de salida.

## F. Schemas
- `supervisor-packet.schema.json` **v2** (estructura pedida: project, review, architecture, contracts, implementation, verification, known_failures, risks, previous/resolved findings, open_questions, evidence_index, attached_evidence, delta). Los paquetes v1 se convierten automáticamente.
- `supervisor-verdict.schema.json` **v2**: PASS · PASS_WITH_ACTIONS · BLOCKED · NEED_EVIDENCE · HUMAN_REVIEW_REQUIRED; severidades CRITICAL…INFO; `evidence_requests` tipados; `required_actions` con `blocking`; `confidence`. Compatible con modo estricto (todo `required`, `additionalProperties:false`, opcionales como `null`).
- Nuevos: `supervisor-review-record`, `supervisor-state`, `supervisor-override`, `supervisor-profile`. v1 queda en `schemas/v1/`.

## G. OpenAI API
Documentación oficial consultada el 2026-11-03: API **Responses** (`POST /v1/responses`) recomendada para proyectos nuevos; salida estructurada con `text.format = {type: json_schema, name, schema, strict: true}`; `reasoning.effort` (valores dependen del modelo: none/minimal/low/medium/high/xhigh/max); `max_output_tokens` incluye tokens de razonamiento y si se agota la respuesta queda `status: incomplete` con `incomplete_details.reason = max_output_tokens`; `usage.input_tokens`, `input_tokens_details.cached_tokens`, `output_tokens`, `output_tokens_details.reasoning_tokens`. `store: false` para no guardar la conversación. `health_check` usa `GET /v1/models/{model}` (no consume tokens). El modelo **no** se fija en código: `SUPERVISOR_MODEL` (o `OPENAI_MODEL`, compatibilidad). El precio no se calcula salvo que el PO configure tarifas oficiales.

## H. GitHub
Nunca en cada push. Workflow reutilizable con `workflow_call`, llamado por el proyecto con `workflow_dispatch` (gate manual) o por etiqueta `supervisor-gate` en un PR del mismo repo. `concurrency` por repo+gate sin cancelar. Permisos: `contents: read`, `pull-requests: write` (comentario). El registro sale como artefacto; persistirlo en el repo es un paso explícito (commit por Claude o PO). Un job gratuito `trigger-check` en CI solo **avisa** qué triggers activa un PR (por rutas), sin llamar al supervisor.

## I. Secretos
`OPENAI_API_KEY` solo por entorno/GitHub Secret; `.env.example` con placeholder; el adaptador nunca la registra; `redact()` en todo log, error y registro; escaneo de secretos en paquete y en evidencia antes de enviar; `.env` en `.gitignore`; un test garantiza que la clave no aparece en ningún archivo del registro.

## J. Cost guards
`SUPERVISOR_MAX_CALLS_PER_GATE` (6), `SUPERVISOR_MAX_EVIDENCE_ROUNDS` (2), `SUPERVISOR_MAX_DELTA_ROUNDS` (3), `SUPERVISOR_MAX_CONTEXT_BYTES` (120 000), `SUPERVISOR_MAX_FILES_PER_REQUEST` (8), `SUPERVISOR_MAX_EVIDENCE_BYTES_PER_FILE` (20 000), `SUPERVISOR_MAX_OUTPUT`, idempotencia, trigger obligatorio, verificación verde antes de gastar. Excedido → STOP + `HUMAN_REVIEW_REQUIRED`.

## K. Modos de fallo
| Fallo | Estado | Gate |
|---|---|---|
| red, 5xx, 429, timeout (tras reintentos) | SUPERVISOR_UNAVAILABLE → SUPERVISOR_PENDING | abierto |
| 401/403/404/400 (config) | SUPERVISOR_UNAVAILABLE (sin reintento) | abierto |
| JSON inválido / fuera de schema / refusal / incomplete | INVALID_SUPERVISOR_RESPONSE (tras 1 reparación) | abierto |
| evidencia insuficiente tras 2 rondas | HUMAN_REVIEW_REQUIRED | abierto |
| límites excedidos | HUMAN_REVIEW_REQUIRED | abierto |
| paquete inválido o con secretos | INVALID_PACKET (no se envía) | abierto |
| lock tomado | LOCKED (no se llama) | sin cambio |
| mismo proyecto+commit+gate+hash | DUPLICATE (no se llama) | sin cambio |
Nunca se marca PASS sin un veredicto válido persistido.

## L. Plan de pruebas
Mock: PASS, PASS_WITH_ACTIONS (con/sin acciones bloqueantes), BLOCKED, NEED_EVIDENCE (1 y 3 rondas), API_FAILURE, INVALID_RESPONSE, TIMEOUT, HUMAN_REVIEW. OpenAI con transporte falso: forma exacta de la petición, reintentos/backoff, 4xx, `incomplete`, `refusal`, uso/tokens. Seguridad: secretos en paquete/evidencia/logs, path traversal, symlinks, evidencia gigante, JSON malformado, inyección en evidencia, lock concurrente, duplicados. Delta, desacuerdo, override, `status`, triggers. Todo sin red.

## M. Archivos a crear
`factory/**` (paquete), `config/supervisor.defaults.json`, `prompts/supervisor/SUPERVISOR_v2.md`, `schemas/supervisor-{review-record,state,override,profile}.schema.json`, `schemas/v1/*`, `templates/SUPERVISOR_PROFILE.json`, `templates/SUPERVISOR_DELTA_PACKET.json`, `.env.example`, `docs/supervisor/*.md`, `tests/test_factory_*.py`.

## N. Archivos a modificar
`schemas/supervisor-packet.schema.json` y `supervisor-verdict.schema.json` (v2), `adapters/supervisor/supervisor.py` (envoltorio compatible), `prompts/CHATGPT_SUPERVISOR.md` (apunta a v2), `policies/SUPERVISOR.md`, `.github/workflows/*.yml`, `templates/SUPERVISOR_PACKET.json`, `tools/validate.py`, `README.md`, `FACTORY.md`, `VERSION` (1.1.0), `CHANGELOG.md`, `tests/test_supervisor.py`, `.gitignore`.
GrafiArts (solo `.factory/`, sin código): `.factory/supervisor.json`, `.factory/reviews/grafiarts/BASELINE/…`, puntero en `PROJECT_STATE.json`.

## O. Riesgos
1. **Conflicto ADR-009 en GrafiArts:** `main` tiene ADR-009 = "clonar CorelDRAW 27.2" y el PR #1 (`factory/f1-bootstrap`) usa ADR-009 = "adopción de la fábrica" y borra `ROADMAP.md` por estar basado en un `main` anterior. Hay que renumerar la adopción como **ADR-010** al integrar.
2. `fabricasoftware` PR #1 sin fusionar: este trabajo se apila sobre él.
3. Los nombres de modelo cambian; por eso el modelo es configuración del PO.
4. La API puede cambiar campos; el adaptador tolera campos extra y falla cerrado.
5. El supervisor no ve el repo: un paquete sesgado lo engaña. Mitigación: evidencia verificable, delta con `findings_addressed`, prohibición explícita en la política.
6. Coste real desconocido hasta la primera llamada (se mide el tamaño en bytes).

## P. Implementación por etapas
1. Schemas v2 + config + prompt v2.
2. Adapters (base, mock, manual, OpenAI) + política.
3. Store (IDs, idempotencia, lock) + orquestador + evidencia + delta + desacuerdo + override.
4. CLI `factory supervisor …` y `factory status` + triggers.
5. Tests Mock y de seguridad; CI.
6. Workflow GitHub + documentación.
7. Perfil GrafiArts + paquete BASELINE (sin enviar).
8. STOP → `READY_FOR_LIVE_SUPERVISOR_TEST`.

**Autorización:** `FACTORY.md` define que los cambios a la fábrica entran como PR versionado; el PO lo pidió y nada de esto toca un gate de GrafiArts ni contratos congelados. La llamada real queda fuera hasta su autorización.
