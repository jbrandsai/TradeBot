# Python Trading Engine

A Python-based trading application built to connect with broker APIs, process market/account data, apply risk controls, generate trade decisions, and execute orders in controlled modes. This project was designed to simulate a production-minded trading workflow with a strong focus on safety, logging, configuration, and maintainability.

## Overview

This application performs a full trading run from start to finish:

1. Loads settings from a configuration file
2. Reads safety controls such as kill switch and read-only mode
3. Connects to broker/account data
4. Pulls current equity, positions, and market information
5. Computes strategy targets or signals
6. Passes signals through a risk manager
7. Generates orders within defined limits
8. Executes in dry-run, paper, or live-controlled modes
9. Writes logs and audit records for traceability

The project was built in VS Code using Python and is structured around safety-first execution and clear operational visibility.

## Features

- Broker/API integration
- Configuration-driven execution
- Safety controls including kill switch and read-only mode
- Risk management layer before order execution
- Support for dry-run, paper, and live execution modes
- Logging for scheduler runs, equity checks, and trade activity
- Audit-friendly output and execution records
- Web/API status visibility for monitoring
- Manual run controls and scheduling support

## Tech Stack

- Python
- Flask
- REST APIs
- JSON
- YAML
- CSV
- SQL concepts
- VS Code
- Windows Task Scheduler
- PowerShell

## Project Structure

```text
python-trading-engine/
│
├── app/
│   └── api_server.py
├── logs/
│   └── scheduler_trading.log
├── scheduled_jobs/
│   ├── run_trading_daily.bat
│   ├── run_hotpicks_daily.bat
│   └── record_equity_daily.bat
├── config.yaml
├── trade_log.csv
├── equity_history.csv
├── run_trading_once.py
├── run_hotpicks.py
├── record_equity.py
└── README.md
