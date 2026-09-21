"""Read the tri-an report inputs from Google Sheets and build a LINE summary.

The report intentionally uses a service account with read-only access. It does
not make any change to the source workbook.
"""

from __future__ import annotations

import base64
import json
import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
from google.oauth2 import service_account
from googleapiclient.discovery import build
from PIL import Image, ImageDraw, ImageFont


class ReportDataError(ValueError):
    """A source sheet is missing a required tab, header, or usable data."""


@dataclass(frozen=True)
class TriAnReport:
    report_date: pd.Timestamp
    summary: str
    base: pd.DataFrame


ORIGINS = ("NỘI ĐỊA", "NHẬP KHẨU")
SHEETS_SCOPE = ("https://www.googleapis.com/auth/spreadsheets.readonly",)


def build_tri_an_summary(requested_date: str | None = None) -> TriAnReport:
    spreadsheet_id = required_env("GOOGLE_SHEET_ID")
    service = sheets_service()
    sales = read_table(service, spreadsheet_id, env_tab("GOOGLE_SHEET_SALES_TAB", "so_ban"))
    inventory = read_table(service, spreadsheet_id, env_tab("GOOGLE_SHEET_INVENTORY_TAB", "so_ton"))
    stores = read_table(service, spreadsheet_id, env_tab("GOOGLE_SHEET_STORES_TAB", "sieu_thi"))
    products = read_table(service, spreadsheet_id, env_tab("GOOGLE_SHEET_PRODUCTS_TAB", "sp"))

    stores = prepare_stores(stores)
    products = prepare_products(products)
    inventory, report_date = prepare_inventory(inventory, requested_date)
    sales = prepare_sales(sales)
    store_ids = set(stores["Mã siêu thị"]) - {""}
    model_ids = set(products["Mã Model"]) - {""}
    inventory = inventory[(inventory["Ngày"] == report_date) & inventory["Mã siêu thị"].isin(store_ids) & inventory["Mã Model"].isin(model_ids)]
    sales = sales[(sales["Ngày"] == report_date) & sales["Mã siêu thị"].isin(store_ids) & sales["Mã Model"].isin(model_ids)]

    inventory = inventory.merge(products, on="Mã Model", how="inner")
    sales = sales.merge(products, on="Mã Model", how="inner")
    stock_by_store = inventory.groupby(["Mã siêu thị", "Loại NCC"], as_index=False)["Tồn kho"].sum().rename(columns={"Tồn kho": "Số tồn"})
    sold_by_store = sales.groupby(["Mã siêu thị", "Loại NCC"], as_index=False)["Số bán"].sum()
    base = cross_join_stores_and_origins(stores)
    base = base.merge(stock_by_store, on=["Mã siêu thị", "Loại NCC"], how="left")
    base = base.merge(sold_by_store, on=["Mã siêu thị", "Loại NCC"], how="left")
    base[["Số tồn", "Số bán"]] = base[["Số tồn", "Số bán"]].fillna(0)
    return TriAnReport(
        report_date=report_date,
        summary=format_summary(base, report_date),
        base=base,
    )


def sheets_service():
    encoded_key = required_env("GOOGLE_SERVICE_ACCOUNT_JSON_B64")
    try:
        key_info = json.loads(base64.b64decode(encoded_key).decode("utf-8"))
    except Exception as error:  # noqa: BLE001
        raise ReportDataError("GOOGLE_SERVICE_ACCOUNT_JSON_B64 không phải JSON key Base64 hợp lệ.") from error
    credentials = service_account.Credentials.from_service_account_info(key_info, scopes=SHEETS_SCOPE)
    return build("sheets", "v4", credentials=credentials, cache_discovery=False)


def read_table(service: Any, spreadsheet_id: str, tab_name: str) -> pd.DataFrame:
    try:
        response = service.spreadsheets().values().get(spreadsheetId=spreadsheet_id, range=quoted_tab_range(tab_name)).execute()
    except Exception as error:  # noqa: BLE001
        raise ReportDataError(f"Không đọc được tab '{tab_name}'. Kiểm tra tab tồn tại và đã share Sheet cho Service Account quyền Viewer.") from error
    values = response.get("values", [])
    if len(values) < 2:
        raise ReportDataError(f"Tab '{tab_name}' chưa có dữ liệu hoặc thiếu hàng tiêu đề.")
    headers = make_unique_headers([str(value).strip() for value in values[0]])
    rows = [row + [""] * (len(headers) - len(row)) for row in values[1:]]
    return pd.DataFrame(rows, columns=headers)


def make_unique_headers(headers: list[str]) -> list[str]:
    """Match pandas' ``.1`` convention for duplicate Google Sheets headers."""
    seen: dict[str, int] = {}
    unique: list[str] = []
    for header in headers:
        count = seen.get(header, 0)
        unique.append(header if count == 0 else f"{header}.{count}")
        seen[header] = count + 1
    return unique


def quoted_tab_range(tab_name: str) -> str:
    return "'" + tab_name.replace("'", "''") + "'"


def prepare_stores(frame: pd.DataFrame) -> pd.DataFrame:
    frame = select_and_rename(frame, {"Mã siêu thị": ("Siêu thị tính sức bán", "Mã siêu thị"), "Tên siêu thị": ("Siêu thị tính sức bán.1", "Tên siêu thị"), "Miền": ("Miền",), "RSM": ("RSM",), "QLTP": ("QLTP",)}, "sieu_thi")
    for column in frame.columns:
        frame[column] = clean_text(frame[column])
    return frame.drop_duplicates("Mã siêu thị")


def prepare_products(frame: pd.DataFrame) -> pd.DataFrame:
    frame = select_and_rename(frame, {"Mã Model": ("Mã Model",), "Mã sản phẩm": ("Mã sản phẩm", "Mã hàng"), "Tên sản phẩm": ("Tên sản phẩm", "Tên hàng"), "Loại NCC": ("LOẠI NCC", "Loại NCC")}, "sp")
    frame["Mã Model"] = clean_id(frame["Mã Model"])
    frame["Mã sản phẩm"] = clean_id(frame["Mã sản phẩm"])
    frame["Tên sản phẩm"] = clean_text(frame["Tên sản phẩm"])
    frame["Loại NCC"] = normalize_origin(frame["Loại NCC"])
    frame = frame[frame["Loại NCC"].isin(ORIGINS)]
    if frame.empty:
        raise ReportDataError("Tab 'sp' không có sản phẩm loại NCC NỘI ĐỊA hoặc NHẬP KHẨU.")
    return frame.drop_duplicates("Mã Model")


def prepare_inventory(frame: pd.DataFrame, requested_date: str | None) -> tuple[pd.DataFrame, pd.Timestamp]:
    frame = select_and_rename(frame, {"Ngày nguồn": ("Tồn đầu ngày", "Ngày"), "Mã siêu thị": ("Mã siêu thị",), "Mã Model": ("Mã Model",), "Tồn kho": ("Tồn kho siêu thị", "Số tồn", "Tồn kho")}, "so_ton")
    frame["Ngày"] = parse_date(frame["Ngày nguồn"])
    report_date = resolve_report_date(requested_date, frame["Ngày"])
    frame["Mã siêu thị"] = clean_id(frame["Mã siêu thị"])
    frame["Mã Model"] = clean_id(frame["Mã Model"])
    frame["Tồn kho"] = positive_number(frame["Tồn kho"])
    return frame, report_date


def prepare_sales(frame: pd.DataFrame) -> pd.DataFrame:
    frame = select_and_rename(frame, {"Ngày nguồn": ("Ngày",), "Mã siêu thị nguồn": ("Mã siêu thị",), "Mã Model": ("Mã Model",), "Tổng số lượng": ("Tổng số lượng",), "SL xuất km": ("SL xuất km",)}, "so_ban")
    frame["Ngày"] = parse_date(frame["Ngày nguồn"])
    frame["Mã siêu thị"] = clean_id(frame["Mã siêu thị nguồn"].astype("string").str.split("-", n=1).str[0])
    frame["Mã Model"] = clean_id(frame["Mã Model"])
    frame["Số bán"] = positive_number(frame["Tổng số lượng"]) + positive_number(frame["SL xuất km"])
    return frame


def select_and_rename(frame: pd.DataFrame, mapping: dict[str, tuple[str, ...]], tab_name: str) -> pd.DataFrame:
    normalized = {normalize_header(column): column for column in frame.columns}
    selected: dict[str, pd.Series] = {}
    missing: list[str] = []
    for destination, aliases in mapping.items():
        source = next((normalized.get(normalize_header(alias)) for alias in aliases if normalize_header(alias) in normalized), None)
        if source is None:
            missing.append(" / ".join(aliases))
        else:
            selected[destination] = frame[source]
    if missing:
        raise ReportDataError(f"Tab '{tab_name}' thiếu cột: {', '.join(missing)}.")
    return pd.DataFrame(selected)


def normalize_header(value: str) -> str:
    normalized = unicodedata.normalize("NFD", str(value).strip().casefold())
    normalized = "".join(char for char in normalized if unicodedata.category(char) != "Mn")
    return re.sub(r"\s+", " ", normalized.replace("đ", "d"))


def clean_id(series: pd.Series) -> pd.Series:
    return series.astype("string").fillna("").str.strip().str.replace(r"\.0$", "", regex=True)


def clean_text(series: pd.Series) -> pd.Series:
    return series.astype("string").fillna("").str.strip().str.replace(r"\s+", " ", regex=True)


def normalize_origin(series: pd.Series) -> pd.Series:
    value = clean_text(series).str.upper()
    return value.mask(value.str.contains("NHẬP", na=False), "NHẬP KHẨU").mask(value.str.contains("NỘI", na=False), "NỘI ĐỊA")


def parse_date(series: pd.Series) -> pd.Series:
    text = clean_id(series)
    parsed = pd.to_datetime(text, format="%Y%m%d", errors="coerce")
    return parsed.fillna(pd.to_datetime(series, errors="coerce")).dt.normalize()


def resolve_report_date(value: str | None, dates: pd.Series) -> pd.Timestamp:
    if value:
        try:
            return pd.Timestamp(pd.to_datetime(value, errors="raise")).normalize()
        except (TypeError, ValueError) as error:
            raise ReportDataError("Ngày báo cáo phải theo dạng YYYY-MM-DD, ví dụ 2026-09-15.") from error
    available = dates.dropna()
    if available.empty:
        raise ReportDataError("Tab 'so_ton' không có ngày tồn kho hợp lệ.")
    return pd.Timestamp(available.max()).normalize()


def positive_number(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce").fillna(0).clip(lower=0)


def cross_join_stores_and_origins(stores: pd.DataFrame) -> pd.DataFrame:
    return stores.assign(_key=1).merge(pd.DataFrame({"Loại NCC": ORIGINS, "_key": 1}), on="_key").drop(columns="_key")


def format_summary(base: pd.DataFrame, report_date: pd.Timestamp) -> str:
    total_stores = base["Mã siêu thị"].nunique()
    totals = base.groupby("Loại NCC", as_index=False)[["Số tồn", "Số bán"]].sum()
    regional = base.groupby(["Loại NCC", "Miền"], as_index=False).agg(SLST=("Mã siêu thị", "nunique"), **{"Số tồn": ("Số tồn", "sum"), "Số bán": ("Số bán", "sum")}).sort_values(["Loại NCC", "Số bán"], ascending=[True, False])
    lines = [f"Báo cáo tri ân ngày {report_date:%d/%m/%Y}", f"Phạm vi: {total_stores:,} siêu thị.", "", "Tổng quan:"]
    for _, row in totals.iterrows():
        stock = float(row["Số tồn"])
        sold = float(row["Số bán"])
        ratio = sold / stock if stock else 0
        lines.append(
            f"- {row['Loại NCC']}: tồn {stock:,.0f}; bán {sold:,.0f}; bán/tồn {ratio:.1%}."
        )
    lines.extend(["", "Theo miền:"])
    for _, row in regional.iterrows():
        stock = float(row["Số tồn"])
        sold = float(row["Số bán"])
        ratio = sold / stock if stock else 0
        lines.append(
            f"- {row['Loại NCC']} | {row['Miền']}: {int(row['SLST']):,} ST, "
            f"tồn {stock:,.0f}, bán {sold:,.0f}, {ratio:.1%}."
        )
    return "\n".join(lines)[:4900]


def render_report_image(report: TriAnReport, output_path: Path) -> None:
    """Render the two report tables as a readable PNG for a LINE message."""
    tables = [display_rows(report.base, origin) for origin in ORIGINS]
    title_height = 105
    section_heights = [64 + 56 + len(rows) * 38 for rows in tables]
    width = 1740
    height = title_height + sum(section_heights) + 95
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    font_title = load_font(26, bold=True)
    font_section = load_font(20, bold=True)
    font_header = load_font(15, bold=True)
    font_body = load_font(15)
    font_bold = load_font(15, bold=True)
    draw.text(
        (width // 2, 33),
        f"DỰ ÁN TRI ÂN BÁNH TƯƠI - BC TỒN KHO ĐẦU KỲ & SỐ BÁN REALTIME {report.report_date:%d-%m-%Y}",
        font=font_title,
        fill="#13251e",
        anchor="ma",
    )
    top = title_height
    for origin, rows, section_height in zip(ORIGINS, tables, section_heights, strict=True):
        draw_table(
            draw=draw,
            top=top,
            rows=rows,
            origin=origin,
            width=width,
            section_height=section_height,
            font_section=font_section,
            font_header=font_header,
            font_body=font_body,
            font_bold=font_bold,
        )
        top += section_height + 28
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path, format="PNG", optimize=True)


def display_rows(base: pd.DataFrame, origin: str) -> list[dict[str, Any]]:
    part = base[base["Loại NCC"] == origin].copy()
    grouped = (
        part.groupby(["Miền", "RSM"], as_index=False)
        .agg(SLST=("Mã siêu thị", "nunique"), **{"Số tồn": ("Số tồn", "sum"), "Số bán": ("Số bán", "sum")})
    )
    order = {"HCM": 0, "Miền Bắc": 1, "Miền Tây": 2, "Miền Trung": 3}
    rows: list[dict[str, Any]] = []
    for region, region_part in sorted(grouped.groupby("Miền", sort=False), key=lambda item: (order.get(item[0], 99), item[0])):
        region_part = region_part.sort_values("Số bán", ascending=False)
        for index, (_, row) in enumerate(region_part.iterrows()):
            rows.append(metric_row(region if index == 0 else "", str(row["RSM"]), row, "detail"))
        subtotal = region_part[["SLST", "Số tồn", "Số bán"]].sum()
        rows.append(metric_row("", f"{region} Total", subtotal, "subtotal"))
    overall = grouped[["SLST", "Số tồn", "Số bán"]].sum()
    rows.append(metric_row("", "Grand Total", overall, "grand_total"))
    return rows


def metric_row(region: str, rsm: str, value: pd.Series, row_type: str) -> dict[str, Any]:
    stores = int(value["SLST"])
    stock = float(value["Số tồn"])
    sold = float(value["Số bán"])
    return {
        "region": region,
        "rsm": rsm,
        "stores": stores,
        "stock": stock,
        "sold": sold,
        "stock_per_store": stock / stores if stores else 0,
        "sold_per_store": sold / stores if stores else 0,
        "ratio": sold / stock if stock else 0,
        "type": row_type,
    }


def draw_table(
    draw: ImageDraw.ImageDraw,
    top: int,
    rows: list[dict[str, Any]],
    origin: str,
    width: int,
    section_height: int,
    font_section: ImageFont.ImageFont,
    font_header: ImageFont.ImageFont,
    font_body: ImageFont.ImageFont,
    font_bold: ImageFont.ImageFont,
) -> None:
    left = 38
    right = width - 38
    table_width = right - left
    col_widths = [170, 260, 105, 220, 195, 215, 225, 160]
    scale = table_width / sum(col_widths)
    col_widths = [round(value * scale) for value in col_widths]
    col_widths[-1] += table_width - sum(col_widths)
    x_positions = [left]
    for column_width in col_widths:
        x_positions.append(x_positions[-1] + column_width)
    accent = "#59c99c" if origin == "NỘI ĐỊA" else "#13a4cc"
    header = "#123d30"
    draw.rectangle((left, top, right, top + 64), fill=accent, outline="#111111", width=2)
    draw.text(((left + right) // 2, top + 32), origin, font=font_section, fill="white", anchor="mm")
    header_top = top + 64
    header_bottom = header_top + 56
    labels = [
        "Miền",
        "RSM",
        "SLST",
        "Tồn đầu kỳ kho\nsiêu thị",
        "Tổng SL bán\nđến now",
        "Tồn đầu kỳ Trung\nBình / ST",
        "Trung bình SL bán\nđến now / ST",
        "Tỷ lệ bán / tồn",
    ]
    for index, label in enumerate(labels):
        draw.rectangle((x_positions[index], header_top, x_positions[index + 1], header_bottom), fill=header, outline="#111111", width=2)
        multiline_center(draw, label, (x_positions[index] + x_positions[index + 1]) // 2, header_top + 28, font_header, "white")
    row_top = header_bottom
    for row in rows:
        row_bottom = row_top + 38
        fill, text_color = row_style(row["type"])
        for index in range(len(labels)):
            cell_fill = ratio_color(row["ratio"]) if index == 7 and row["type"] == "detail" else fill
            draw.rectangle((x_positions[index], row_top, x_positions[index + 1], row_bottom), fill=cell_fill, outline="#293742", width=1)
        values = [
            row["region"],
            row["rsm"],
            f"{row['stores']:,}",
            f"{row['stock']:,.0f}",
            f"{row['sold']:,.0f}",
            f"{row['stock_per_store']:,.0f}",
            f"{row['sold_per_store']:,.0f}",
            f"{row['ratio']:.0%}",
        ]
        for index, value in enumerate(values):
            align = "center" if index != 1 else "left"
            draw_cell_text(draw, value, x_positions[index], x_positions[index + 1], row_top, row_bottom, font_bold if row["type"] != "detail" else font_body, text_color, align)
        row_top = row_bottom
    draw.rectangle((left, top, right, top + section_height), outline="#111111", width=2)


def multiline_center(draw: ImageDraw.ImageDraw, value: str, x: int, y: int, font: ImageFont.ImageFont, fill: str) -> None:
    draw.multiline_text((x, y), value, font=font, fill=fill, anchor="mm", align="center", spacing=1)


def draw_cell_text(
    draw: ImageDraw.ImageDraw,
    value: str,
    left: int,
    right: int,
    top: int,
    bottom: int,
    font: ImageFont.ImageFont,
    fill: str,
    align: str,
) -> None:
    y = (top + bottom) // 2
    if align == "left":
        draw.text((left + 8, y), value, font=font, fill=fill, anchor="lm")
    else:
        draw.text(((left + right) // 2, y), value, font=font, fill=fill, anchor="mm")


def row_style(row_type: str) -> tuple[str, str]:
    if row_type == "grand_total":
        return "#d6ebc4", "#b51818"
    if row_type == "subtotal":
        return "#dbe7f2", "#c61f1f"
    return "#ffffff", "#111111"


def ratio_color(ratio: float) -> str:
    if ratio < 0.03:
        return "#f77b73"
    if ratio < 0.06:
        return "#f6ad76"
    if ratio < 0.10:
        return "#f4e78b"
    return "#9fce94"


def load_font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    candidates = (
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        ("C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf"),
    )
    for candidate in candidates:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size=size)
    return ImageFont.load_default()


def env_tab(name: str, default: str) -> str:
    return os.getenv(name, default).strip() or default


def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ReportDataError(f"Thiếu biến Railway: {name}.")
    return value
