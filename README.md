# Desktop Monitor Pipeline

A local Windows desktop app that monitors one or more windows, captures screenshots on a schedule, sends them to an AI vision gateway, validates structured output, and writes records into your database.

## What It Supports Now

- Multiple monitoring tasks in parallel
- AI structured extraction per task:
  - `AI Structured`: screenshot -> vision model -> JSON -> local validation -> database
- OpenAI-compatible gateway protocols:
  - `chat/completions`
  - `/v1/responses`
- Custom prompt, output schema, and validation rules
- Retry-on-validation-failure for AI extraction
- Five-sample schema wizard:
  - capture 5 screenshots
  - run 5 AI structured extractions
  - infer candidate fields and SQL types
  - create the table automatically
  - use automatic write mappings
- insert write mode (default and recommended)
- Screenshot preview, latest structured result, raw model output, sample results, runtime logs
- Local config persistence

## Architecture

```text
ui (MainWindow, MonitorWorker)
  -> core (MonitorPipeline, structured_extraction)
    -> infra.window (WindowService)
    -> infra.capture (WindowCaptureService)
    -> infra.llm (OpenAIGatewayClient)
    -> infra.db (SqlAlchemyMappedRepository, SqlAlchemySchemaManager)
  -> domain (AppSettings, MonitorJob, AiGatewayConfig, DbFieldMapping)
```

## Quick Start

```powershell
cd D:
emote_imge\desktop_monitor
conda activate base
pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -e .
desktop-monitor
```

If `desktop-monitor` is not available, run:

```powershell
$env:PYTHONPATH="D:
emote_imge\desktop_monitor\src"
python -m desktop_monitor.main
```

## Recommended First-Time Flow

1. Create a task and choose a target window.
2. Set `Parse Mode` to `AI Structured`.
3. Fill in gateway protocol, base URL, API key, model, prompts, output schema, and validation rules.
4. Run `Precheck` and `Test Gateway`.
5. Run `Collect 5 Samples` in the `5-Sample Schema` tab.
6. Review the 5 JSON outputs and the inferred schema draft.
7. Click `Create Table`.
8. Run `Write DB` for a single end-to-end insert test.
9. Start monitoring.

## AI Structured Mode

### Gateway Config

- `Gateway`: choose `chat/completions` or `/v1/responses`
- `Base URL`: compatible gateway root, usually ending in `/v1`
- `API Key`: optional for local gateways without auth
- `Model`: vision-capable model name
- `Timeout`, `Retry`, `Temperature`, `Max Output`

### Prompt and Output Controls

- `System Prompt`: role and output discipline
- `Extraction Prompt`: what fields to extract and how to treat missing values
- `Output Spec`: either a JSON example or a full JSON Schema
- `Validation Rules`: local JSON validation after the model returns data

### Validation Rules Example

```json
{
  "required_fields": ["order_no", "amount"],
  "non_empty_fields": ["order_no"],
  "field_types": {
    "amount": "number",
    "created_at": "datetime"
  },
  "regex_rules": {
    "order_no": "^[A-Z0-9-]+$"
  },
  "numeric_ranges": {
    "amount": {"min": 0}
  }
}
```

If validation fails, the app re-prompts the model with the validation feedback until it succeeds or the retry limit is reached.

## Extraction Mode

This project now runs in AI structured extraction mode only.
Legacy regex/OCR settings in old config files are ignored by runtime.

## Database Write Mapping

Manual mapping is no longer required in the UI.
The app now auto-generates mappings at write time by matching:

- system fields -> same-name metadata columns when present
- parsed fields -> schema draft column names (or same-name columns from output schema)

### Source types (internal)

- `parsed`: fields from AI JSON output
- `system`: built-in metadata
- `constant`: fixed value

### Supported system keys

- `captured_at`
- `window_hwnd`
- `window_title`
- `screenshot_path`
- `raw_text`
- `job_id`
- `job_name`
- `parsed_json`
- `parse_mode`
- `model_name`
- `gateway_protocol`
- `attempt_count`
- `validation_json`

## Auto-Created Table Columns

When you create a table from the schema wizard, the app adds these metadata columns automatically:

- `id`
- `captured_at`
- `job_id`
- `job_name`
- `window_hwnd`
- `window_title`
- `screenshot_path`
- `raw_text`
- `parse_mode`
- `model_name`
- `gateway_protocol`
- `attempt_count`
- `validation_errors`

It then appends the inferred parsed fields from the 5-sample run.

## Notes

- Window capture is still coordinate-based screen capture. If the target window is covered by another window, extraction can be wrong.
- The AI gateway must support image input for the selected protocol.
- Some third-party gateways may partially support OpenAI-compatible structured outputs. The client will attempt a compatibility fallback without schema enforcement if needed.
- Existing legacy single-task configs are still loadable.
