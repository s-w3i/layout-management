# Setup and deployment

## Prerequisites

Install these on the machine that will run the application:

- Node.js 22.23.4 or later, with npm and its built-in SQLite module. The bundled SQLite library must be 3.51.3 or later.
- CMake 3.16 or later.
- A C++20 compiler, such as GCC 11 or later.
- Git, Git LFS and internet access for dependency and dataset downloads.

On Ubuntu/Debian, install the build tools:

```bash
sudo apt-get update
sudo apt-get install -y build-essential cmake git git-lfs
git lfs install
```

Install Node.js separately if necessary, then check its version:

```bash
node --version
npm --version
node --input-type=module -e 'import { DatabaseSync } from "node:sqlite"; const db = new DatabaseSync(":memory:"); console.log(db.prepare("SELECT sqlite_version() AS version").get()); db.close();'
```

## Checkout and build

```bash
git clone --branch web-based --single-branch https://github.com/s-w3i/layout-management.git layout_generator
cd layout_generator
git lfs pull
sha256sum --check resources/SHA256SUMS
npm ci --include=dev
npm run build
```

Run these commands from the project directory.

## Start and stop

```bash
npm start
```

Open **http://127.0.0.1:4317**. Keep the terminal open; press **Ctrl+C** to stop the server.

Alternatively, the launcher installs missing dependencies, builds and starts the application:

```bash
./scripts/run.sh
```

To use another port:

```bash
PORT=4318 ./scripts/run.sh
```

Open **http://127.0.0.1:4318** when using that port. If a port is already occupied, stop the existing server in its terminal or choose a different port before starting another instance.

Use a browser with WebGL 2 support for the 3D view.

## Load the supplied data

During warehouse setup, import a `.grid.json` file from `resources/map/amr/` or `resources/map/asrs/`. Load the original input files from `resources/data/` and `resources/others/` for demand and stock setup.

## Initialize the database and integration access

The first start creates `.data/app.sqlite` and migrates existing workspace and asset JSON files. Keep the original `.data/` directory in place during this first start. Migration preserves the original files.

1. Open **Data & inventory → Overview**, set the warehouse time zone and register the warehouse.
2. On **Master data**, check SKU attributes and UOM conversions.
3. On **Inventory**, enter verified opening counts or import an opening-stock CSV. Calculated stock requirements are planning targets.
4. Finish layout generation, synchronize the warehouse, then open **Publish** to validate and publish a release.
5. On **Integration**, create a read or operate API key and copy it once. Use the [integration setup and API reference](docs/API.md) to connect the consuming system.

Use **Overview → Create backup** to create a consistent SQLite snapshot. Original upload files should also be included in deployment backups.

## Set up ASRS simulation

1. Import the ASRS grid, calculate stock requirements, generate the layout and publish the warehouse.
2. Import the original order file if the selected history has no retained order lines.
3. Open **ASRS simulation**, connect to the deployed warehouse API and select the published warehouse and order history.
4. Choose dates/months, playback speed and replenishment lead time, then press **Prepare simulation → Start**.

Follow the [ASRS setup and operation instructions](docs/ASRS.md) for source connections, playback controls and stock-flow exports. Stop the server and back up the complete `.data/` directory to preserve both warehouse and simulation databases.

To keep runtime data in another directory, set its absolute path when starting:

```bash
DATA_DIR=/absolute/path/to/warehouse-data npm start
```

Use separate data directories for concurrently running application instances.

## Deploy on a remote Linux machine

Complete the prerequisite, checkout and build steps on the remote machine, then start the application there with `npm start`.

The server listens on `127.0.0.1`. From your own computer, open an SSH tunnel, replacing `user@server` with your SSH login:

```bash
ssh -N -L 4317:127.0.0.1:4317 user@server
```

Keep both the application and SSH tunnel running, then open **http://127.0.0.1:4317** on your computer. If using a custom port, use that port on both sides of the tunnel and in the browser URL.

## Back up and update

Stop the server with **Ctrl+C**. After the first run, back up the runtime data before updating:

```bash
tar -czf ../layout-generator-data-backup.tar.gz .data
```

Update, rebuild and restart:

```bash
git pull --ff-only origin web-based
git lfs pull
npm ci --include=dev
npm run build
npm start
```

Keep `.data/` when updating or moving the deployment. To restore the backup into a stopped deployment, run this from the project directory:

```bash
tar -xzf ../layout-generator-data-backup.tar.gz
```
