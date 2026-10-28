# Week 1 review input

This folder contains the code your review tool will inspect. It is not a starter implementation of the tool.

- `order_service.py`: 1,060 lines of Python, using only the standard library. Includes SQLite storage, validation, pricing, inventory, order state, refunds, reports, imports and a CLI.
- `BUSINESS_RULES.md`: expected behavior.
- `REQUESTS.md`: the three shared review requests and reproduction commands.
- `cases/`: small JSON inputs for local runs.

Use Python 3.11 or newer. No installation or API key is needed to run this sample service. Your review tool needs the free-tier inference key described in the assignment.

From this folder:

```sh
python order_service.py --help
python order_service.py --db demo.db --at 2026-10-26T10:00:00+00:00 seed
python order_service.py --db demo.db catalog
python order_service.py --db demo.db quote --request cases/quantity.json
```

The service creates the database only when you run a command. There are no import-time actions or external requests. Use a fresh database filename for independent scenarios. Reusing a seeded database preserves existing orders and stock.

You may manually run these supplied commands to understand the examples. The review tool you build must read code without executing it. Keep this folder's source and cases unchanged in your public Git repository. Store generated databases outside the submitted folder and exclude them with `.gitignore`.

This code and fictional data were authored for the internship exercise and may be copied into the interns' public submissions. Do not use the service for real orders or payments. It contains behavior for you to investigate.
