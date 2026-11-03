# Changelog

## 1.1.0 · 2026-11-03
- Supervisor automático: `python -m factory supervisor review|evidence|delta|retry|baseline|close|override|validate|health|show`, `factory status`, `factory trigger evaluate`.
- `SupervisorAdapter` con `OpenAISupervisorAdapter` (API Responses, salida JSON estricta, `store:false`, reintentos con backoff, health check sin tokens), `MockSupervisorAdapter` y `ManualSupervisorAdapter`.
- Schemas v2 de paquete y veredicto (NEED_EVIDENCE, HUMAN_REVIEW_REQUIRED, INFO, delta, baseline). v1 en `schemas/v1/` y conversión automática. Nuevos: perfil, estado, override.
- Registro reproducible en `.factory/reviews/`, IDs `PREFIJO-MILESTONE-GATE-NNNN`, idempotencia, locks, límites de costo, desacuerdo técnico automático, overrides del PO, telemetría de uso.
- Prompt del supervisor versionado (`prompts/supervisor/SUPERVISOR_v2.md`) con defensa ante inyección.
- Configuración central (`config/supervisor.defaults.json`), `.env.example`, interruptor `SUPERVISOR_LIVE_ENABLED` (por defecto apagado).
- Workflows: gate reutilizable con `concurrency` y entradas por entorno; aviso de triggers por PR.

## 1.0.0 · 2026-11-02
- Primera versión mínima: constitución, 4 políticas, 5 schemas, 4 prompts, plantillas, SupervisorAdapter para OpenAI con operación degradada, validador y workflows (CI de la fábrica y gate reutilizable del supervisor).
- Basada en el "Software Factory Master Protocol" redactado con ChatGPT y en la arquitectura propuesta por Claude; consolidada en menos archivos para empezar a producir antes.
