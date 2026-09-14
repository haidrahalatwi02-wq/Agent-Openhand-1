# Lead Finder Agent

An intelligent lead discovery and qualification system that leverages AI and location-based data to identify and score business leads.

## Features

- **Multi-Source Lead Discovery**: Search across multiple data providers (OpenStreetMap, Google Maps, etc.)
- **Intelligent Lead Qualification**: AI-powered scoring based on business signals and website characteristics
- **Flexible Configuration**: YAML/JSON-based configuration for search parameters and scoring rules
- **Export Capabilities**: Multiple export formats (CSV, JSON, Excel)
- **Extensible Architecture**: Easy to add new search providers and scoring rules

## Quick Start

### Installation

```bash
pip install -r requirements.txt
```

### Configuration

1. Copy `.env.example` to `.env`
2. Fill in your API keys and configuration
3. Customize `config/` files for your search parameters

### Usage

```bash
python -m lead_finder_agent
```

## Documentation

- [Architecture](docs/architecture.md)
- [Configuration Guide](docs/configuration.md)
- [Data Model](docs/data-model.md)
- [Development Guide](docs/development.md)
- [Available Providers](docs/providers.md)
- [Usage Examples](docs/usage.md)

## Project Structure

```
lead_finder_agent/
├── checker/          # Data validation and signal checking
├── config/           # Configuration loading and management
├── core/             # Core agent and pipeline logic
├── extraction/       # Data extraction and normalization
├── models/           # Data models and enums
├── scoring/          # Lead scoring engine
├── search/           # Search providers and aggregation
├── storage/          # Data persistence
└── utils/            # Utility functions
```

## Contributing

Please see [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines on how to contribute to this project.

## License

This project is licensed under the MIT License - see [LICENSE](LICENSE) file for details.

## Support

For issues, questions, or suggestions, please open an issue on GitHub.
