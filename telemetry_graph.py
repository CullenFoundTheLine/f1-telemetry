"""Validate a telemetry CSV and create one speed-versus-time PNG graph.

Required CSV columns (additional columns are allowed):
    time_s,speed_kmh

Time is elapsed seconds; speed is kilometers per hour.
"""

import argparse
import csv
import math
import sys
from pathlib import Path


REQUIRED_COLUMNS = ("time_s", "speed_kmh")


class TelemetryError(ValueError):
    """Raised when a CSV does not meet the expected telemetry format."""


def load_telemetry(csv_path: Path) -> tuple[list[float], list[float]]:
    """Read and validate every sample before returning any data for plotting."""
    times: list[float] = []
    speeds: list[float] = []

    try:
        with csv_path.open("r", encoding="utf-8-sig", newline="") as file:
            reader = csv.reader(file, strict=True)
            header = next(reader, None)
            if header is None:
                raise TelemetryError("The CSV file is empty.")

            columns = [name.strip() for name in header]
            if len(columns) != len(set(columns)):
                raise TelemetryError("The CSV contains duplicate column names.")
            missing = [name for name in REQUIRED_COLUMNS if name not in columns]
            if missing:
                raise TelemetryError(
                    "Missing required column(s): " + ", ".join(missing)
                    + ". Expected 'time_s' and 'speed_kmh'."
                )

            time_index = columns.index("time_s")
            speed_index = columns.index("speed_kmh")

            for row in reader:
                line = reader.line_num
                if len(row) != len(columns):
                    raise TelemetryError(
                        f"Line {line}: expected {len(columns)} values, got {len(row)}."
                    )

                try:
                    time_s = float(row[time_index].strip())
                    speed_kmh = float(row[speed_index].strip())
                except ValueError as exc:
                    raise TelemetryError(
                        f"Line {line}: time_s and speed_kmh must be numeric and nonempty."
                    ) from exc

                if not (math.isfinite(time_s) and math.isfinite(speed_kmh)):
                    raise TelemetryError(f"Line {line}: NaN and infinity are not allowed.")
                if time_s < 0:
                    raise TelemetryError(f"Line {line}: elapsed time cannot be negative.")
                if speed_kmh < 0:
                    raise TelemetryError(f"Line {line}: speed cannot be negative.")
                if times and time_s <= times[-1]:
                    raise TelemetryError(
                        f"Line {line}: timestamps must be strictly increasing "
                        "(no duplicates or backward jumps)."
                    )

                times.append(time_s)
                speeds.append(speed_kmh)
    except (OSError, UnicodeError, csv.Error) as exc:
        raise TelemetryError(f"Cannot read CSV '{csv_path}': {exc}") from exc

    if len(times) < 2:
        raise TelemetryError("At least two valid telemetry samples are required.")

    return times, speeds


def select_csv() -> Path:
    """Open a file picker when no CSV path is supplied on the command line."""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError as exc:
        raise TelemetryError(
            "The file picker is unavailable. Pass a CSV path on the command line."
        ) from exc

    try:
        root = tk.Tk()
        root.withdraw()
        try:
            selected = filedialog.askopenfilename(
                title="Select a telemetry CSV file",
                filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
            )
        finally:
            root.destroy()
    except tk.TclError as exc:
        raise TelemetryError(
            "The file picker is unavailable. Pass a CSV path on the command line."
        ) from exc

    if not selected:
        raise TelemetryError("No CSV file selected.")
    return Path(selected)


def save_graph(
    times: list[float], speeds: list[float], output: Path, show: bool
) -> None:
    """Plot the raw samples in order; produce exactly one labeled graph."""
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(10, 5))
    try:
        ax.plot(times, speeds, linewidth=1.5, color="tab:blue")
        ax.set(
            title="Speed vs. Time",
            xlabel="Elapsed time (s)",
            ylabel="Speed (km/h)",
        )
        ax.set_ylim(bottom=0)
        ax.grid(True, alpha=0.3)
        fig.tight_layout()
        fig.savefig(output, dpi=150)
        print(f"Validated {len(times)} samples. Graph saved to: {output}")
        if show:
            plt.show()
    finally:
        plt.close(fig)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "csv_file", nargs="?", type=Path,
        help="Telemetry CSV path (opens a file picker if omitted).",
    )
    parser.add_argument(
        "-o", "--output", type=Path,
        help="Output PNG path (default: <csv_name>_speed_vs_time.png).",
    )
    parser.add_argument(
        "--no-show", action="store_true",
        help="Save the PNG without opening an interactive graph window.",
    )
    args = parser.parse_args()

    try:
        csv_path = args.csv_file if args.csv_file is not None else select_csv()
        output = (
            args.output if args.output is not None
            else csv_path.with_name(csv_path.stem + "_speed_vs_time.png")
        )
        if output.suffix.lower() != ".png":
            raise TelemetryError("The output filename must end in .png.")
        if csv_path.resolve() == output.resolve():
            raise TelemetryError("The output path cannot be the input CSV path.")

        times, speeds = load_telemetry(csv_path)
        if args.no_show:
            import matplotlib
            matplotlib.use("Agg")
        save_graph(times, speeds, output, show=not args.no_show)
        return 0
    except (TelemetryError, OSError, ImportError, RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
