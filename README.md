# STUDNIA

### AI-Powered Business Automation

**STUDNIA** is a Python-based business automation project focused on e-commerce workflows, web data collection, and integrations with external services.

The project aims to reduce repetitive manual work by connecting automation tools, browser-based workflows, Telegram, and Google Sheets. Its long-term vision is to evolve into a flexible AI-powered platform with customizable agents and business automation workflows.

## Features

- **Web Data Collection** — retrieve and process information from supported websites.
- **E-commerce Automation** — streamline product research and product data workflows.
- **Telegram Bot** — interact with automation features through a Telegram interface.
- **Google Sheets Integration** — store and organize data in spreadsheets.
- **Browser Automation** — automate supported browser-based tasks.
- **OpenSERP Integration** — use a self-hosted search service to support web search workflows.
- **Extensible Architecture** — build additional integrations and automation modules as the project evolves.

## Tech Stack

- Python
- Telegram Bot API
- Playwright
- Docker
- OpenSERP
- Google Sheets API
- Web scraping and data processing
- Environment-based configuration

## Project Structure

```text
STUDNIA/
├── archive/          # Archived experiments and debugging utilities
├── ads.py            # Advertisement-related functionality
├── bot.py            # Telegram bot entry point
├── market.py         # Marketplace-related functionality
├── settings.py       # Application configuration
├── shafa.py          # Shafa-related functionality
├── test_market.py    # Marketplace tests
├── requirements.txt  # Python dependencies
├── .env.example      # Example environment configuration
├── .gitignore        # Git exclusions
└── README.md         # Project documentation
```

## Requirements

Before getting started, make sure you have installed:

- Python 3.10 or later
- Git
- Docker Desktop or Docker Engine
- Access to the required Telegram and Google Sheets credentials

The Python version and other dependencies should be compatible with the project's current code and `requirements.txt`.

## Getting Started

### 1. Clone the repository

```bash
git clone https://github.com/david08d/STUDNIA.git
cd STUDNIA
```

### 2. Create a virtual environment

```bash
python -m venv .venv
```

Activate it on Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
```

### 3. Install Python dependencies

```bash
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### 4. Start OpenSERP with Docker

STUDNIA can use OpenSERP to support web search workflows. Make sure Docker is installed and running.

Start the OpenSERP container:

```bash
docker run --rm -p 127.0.0.1:7000:7000 karust/openserp:latest serve -a 0.0.0.0 -p 7000
```

Keep this terminal open while using OpenSERP.

The service is published on `127.0.0.1:7000` and is intended for local access from the same computer.

To stop the service, press `Ctrl+C` in the terminal running the container.

### 5. Configure environment variables

Create a `.env` file in the project root using `.env.example` as a template.

Add the configuration values required by your setup, such as API tokens, service URLs, and application settings.

Keep all secrets in your local `.env` file.

**Never commit `.env`, API keys, Telegram bot tokens, Google service-account credentials, or other private information to GitHub.**

### 6. Configure Google Sheets and external services

If your workflow requires Google Sheets, configure the appropriate Google API access and credentials.

Make sure the spreadsheet, service account, API permissions, and other required integrations are configured before running features that depend on them.

### 7. Run the Telegram bot

Open a **second terminal** in the project directory, activate the virtual environment, and run:

```bash
python bot.py
```

The bot and its integrations will work only when the required configuration and external services are available.

## Troubleshooting

### OpenSERP is not responding

- Check that Docker is running.
- Verify that the OpenSERP container started without errors.
- Make sure port `7000` is not already occupied.
- Confirm that the application is configured to use the correct local OpenSERP endpoint.

### Python dependencies are missing

Activate the virtual environment and run:

```bash
pip install -r requirements.txt
```

### Configuration errors

- Verify that `.env` exists in the project root.
- Check that the required environment variables are present.
- Ensure that credentials are valid and have the necessary permissions.

## Roadmap

- [ ] Improve automation reliability and error handling.
- [ ] Expand e-commerce and marketplace integrations.
- [ ] Improve product data collection and processing.
- [ ] Develop AI-powered agents for business workflows.
- [ ] Add customizable automation pipelines.
- [ ] Integrate additional platforms and external services.

## Vision

STUDNIA aims to become a flexible business automation ecosystem where entrepreneurs and teams can connect their tools, automate repetitive workflows, and use AI agents to handle increasingly complex tasks.

**STUDNIA — one source for smarter business automation.**

## License

No license has been specified yet. Unless a license is added, all rights remain with the copyright holder. Reuse, distribution, and modification are not automatically permitted.

---

*STUDNIA is under active development. Features, integrations, and setup requirements may change.*