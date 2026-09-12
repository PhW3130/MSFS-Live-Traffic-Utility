# MSFS Live Traffic Utility

MSFS Live Traffic Utility is an open-source Python application that displays real-world ADS-B traffic on an interactive map and can inject matching AI traffic into Microsoft Flight Simulator using SimConnect.

The goal of this project is to provide a lightweight and transparent alternative for displaying and utilizing real-world air traffic within Microsoft Flight Simulator while remaining fully open source.

## Features

- Real-time ADS-B aircraft tracking
- Interactive world map
- Search and track individual flights
- Detailed aircraft information panel
- Altitude-based color gradients
- Flight trail visualization
- Automatic aircraft model mapping
- SimConnect integration
- Experimental AI traffic injection
- Support for FSLTL model matching
- JSON-based theme customization
- Automatic language detection (German / English)

## How It Works

The application retrieves publicly available ADS-B traffic data and displays aircraft positions on a live map.

When Microsoft Flight Simulator is running, the utility can use SimConnect to spawn matching AI aircraft and update their positions based on real-world traffic data.

When the simulator is not running, the utility automatically starts in map mode and can be used as a standalone traffic viewer.

## Requirements

### Map Mode

- Windows 10 / 11
- Python 3.11 or newer
- Internet connection

### AI Traffic Mode

- Microsoft Flight Simulator 2020 or 2024
- SimConnect
- FSLTL Base Models (recommended)
- Internet connection

## Installation

Clone the repository:

```bash
git clone https://github.com/YOURNAME/MSFS-Live-Traffic-Utility.git
cd MSFS-Live-Traffic-Utility
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Start the application:

```bash
python main.py
```

## Releases

Pre-built releases are available on Flightsim.to.

Please download the latest compiled version from the Flightsim.to project page if you do not want to run the software from source.

## Configuration

The user interface can be customized through the `colors.json` file.

Examples:

- Custom tile servers
- Custom UI themes
- Custom altitude color gradients

## Project Status

This project is actively developed and considered experimental.

Some features, especially AI traffic injection and aircraft model matching, are continuously being improved.

Feedback, bug reports and pull requests are always welcome.

## Contributing

Contributions are encouraged.

You can help by:

- Reporting bugs
- Improving aircraft mappings
- Testing new releases
- Creating pull requests
- Suggesting new features

## Data Sources

This project uses publicly available ADS-B data.

Please respect the terms of service and usage policies of the data providers you choose to use.

## License

This project is licensed under the GNU General Public License v3.0 (GPL-3.0).

You are free to use, modify and redistribute the software under the terms of the GPL-3.0 license.

## Disclaimer

This project is not affiliated with or endorsed by Microsoft, Asobo Studio, Flightsim.to, FSLTL, ADS-B Exchange, ADSB.lol or any other third-party organization.

Microsoft Flight Simulator and all related trademarks belong to their respective owners.
