# Enrutamiento de modelos y política de tokens

## Clases de modelo (abstractas)
| Clase | Uso | Modelo actual (verificar disponibilidad) |
|---|---|---|
| DIRECTOR | arquitectura, planificación, integración entre dominios, riesgos, paquetes del supervisor | Claude Opus |
| WORKER | implementación, refactor, debugging, tests, UI, APIs, documentación técnica | Claude Sonnet |
| FAST | clasificación, búsqueda, inventarios, resúmenes, cambios repetitivos | Claude Haiku |
| SUPERVISOR | auditoría independiente en gates | OpenAI (modelo en `OPENAI_MODEL`) |

Las clases son estables; los modelos concretos cambian sin tocar el protocolo (ver `FACTORY.md`, adaptadores).

## Enrutamiento por complejidad y riesgo
| Complejidad | Ruta |
|---|---|
| L0 mecánica | FAST |
| L1 simple | FAST o WORKER |
| L2 ingeniería estándar | WORKER |
| L3 ingeniería compleja | WORKER + revisión del DIRECTOR, o DIRECTOR si hay fuerte interdependencia |
| L4 arquitectura / investigación crítica | DIRECTOR |
| Riesgo CRITICAL (cualquier L) | DIRECTOR + revisión obligatoria del SUPERVISOR |

## Contexto mínimo
Jerarquía: PROJECT_STATE → DOMAIN_STATE → TASK CONTRACT → símbolos → archivos relevantes → diff → evidencia puntual. Solo se amplía si hace falta.
- Nunca cargar todo el repo por comodidad.
- Documentos estables (arquitectura o contratos congelados) se consumen por resumen versionado; si cambia la versión, solo se invalidan los contextos afectados.
- Respuestas de agentes: ≤ 600 palabras salvo que el contrato pida otra cosa.

## Reintentos
1. Intento inicial con el trabajador adecuado.
2. Si falla: un reintento **solo con evidencia nueva** (error concreto, test que falla).
3. Si vuelve a fallar: análisis de causa raíz.
4. Después: escalar de clase si está justificado.
Nunca repetir el mismo prompt.

## Stop-loss
| Estado | Disparador | Acción |
|---|---|---|
| NORMAL | — | seguir |
| WARNING | > 60 % del presupuesto de la tarea sin un criterio cumplido | reducir contexto, revisar plan |
| STOP_AND_SUMMARIZE | > 85 % del presupuesto o 2 fallos | parar y escribir intentos, evidencia, hipótesis, fallos y estado |
| ESCALATE | tras el resumen | DIRECTOR o Product Owner |

Presupuestos por `TOKEN_BUDGET_CLASS`: los fija cada proyecto en su perfil y se ajustan con las métricas reales. No se inventan cifras.

## Reparto diario
El Product Owner fija qué parte del límite diario usa el constructor (Claude) y cuál queda para el supervisor. Valor actual de referencia: constructor hasta ~80 %.

## Telemetría (opcional, no bloquea)
Por tarea: agente, modelo, tarea, tamaño de contexto de entrada, tamaño de salida, duración, reintentos, resultado, escaladas y, si el proveedor lo entrega, tokens y costo. No se inventan costos.

Métricas: éxito a la primera, retrabajo, defectos escapados, tokens y costo por tarea aceptada, reintentos medios, tasa de bloqueo del supervisor, fallos de tests, regresiones y tiempo de ciclo por milestone.
