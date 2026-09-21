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
import fitz
import httpx
from google.auth.transport.requests import Request as GoogleRequest
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
SHEETS_WRITE_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
DRIVE_READ_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
OUTPUT_SHEET_DEFAULT = "output_tri_an"


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


def service_account_credentials(write: bool = False):
    encoded_key = required_env("GOOGLE_SERVICE_ACCOUNT_JSON_B64")
    try:
        key_info = json.loads(base64.b64decode(encoded_key).decode("utf-8"))
    except Exception as error:  # noqa: BLE001
        raise ReportDataError("GOOGLE_SERVICE_ACCOUNT_JSON_B64 không phải JSON key Base64 hợp lệ.") from error
    scopes = (SHEETS_WRITE_SCOPE, DRIVE_READ_SCOPE) if write else SHEETS_SCOPE
    return service_account.Credentials.from_service_account_info(key_info, scopes=scopes)


def sheets_service(write: bool = False):
    return build("sheets", "v4", credentials=service_account_credentials(write), cache_discovery=False)


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


def generate_google_sheet_report_image(report: TriAnReport, output_path: Path) -> str:
    """Write the report to the dedicated output tab, then export that tab to PNG."""
    spreadsheet_id = required_env("GOOGLE_SHEET_ID")
    service = sheets_service(write=True)
    output_sheet_id = write_output_sheet(service, spreadsheet_id, report)
    pdf_bytes = export_output_sheet_pdf(spreadsheet_id, output_sheet_id)
    render_pdf_to_png(pdf_bytes, output_path)
    return output_sheet_name()


def write_output_sheet(service: Any, spreadsheet_id: str, report: TriAnReport) -> int:
    name = output_sheet_name()
    metadata = service.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        fields="sheets(properties(sheetId,title,gridProperties))",
    ).execute()
    properties = next(
        (item["properties"] for item in metadata.get("sheets", []) if item["properties"]["title"] == name),
        None,
    )
    if properties is None:
        response = service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "requests": [
                    {
                        "addSheet": {
                            "properties": {
                                "title": name,
                                "gridProperties": {"rowCount": 100, "columnCount": 14},
                            }
                        }
                    }
                ]
            },
        ).execute()
        sheet_id = int(response["replies"][0]["addSheet"]["properties"]["sheetId"])
        row_count, column_count = 100, 14
    else:
        sheet_id = int(properties["sheetId"])
        grid = properties.get("gridProperties", {})
        row_count = max(int(grid.get("rowCount", 100)), 100)
        column_count = max(int(grid.get("columnCount", 14)), 14)

    layout = output_layout(report)
    used_rows = len(layout["values"])
    service.spreadsheets().values().clear(
        spreadsheetId=spreadsheet_id, range=quoted_tab_range(name), body={}
    ).execute()
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=(
            f"{quoted_tab_range(name)}!"
            f"{column_letter(layout['start_column'] + 1)}{layout['start_row'] + 1}:"
            f"{column_letter(layout['start_column'] + 8)}{layout['start_row'] + used_rows}"
        ),
        valueInputOption="RAW",
        body={"values": layout["values"]},
    ).execute()
    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": output_format_requests(sheet_id, row_count, column_count, layout)},
    ).execute()
    return sheet_id


def output_layout(report: TriAnReport) -> dict[str, Any]:
    values: list[list[Any]] = [
        [f"DỰ ÁN TRI ÂN BÁNH TƯƠI - BC TỒN KHO ĐẦU KỲ & SỐ BÁN REALTIME {report.report_date:%d-%m-%Y}"] + [""] * 7,
        [""] * 8,
    ]
    sections: list[dict[str, Any]] = []
    labels = ["Miền", "RSM", "SLST", "Tồn đầu kỳ kho\nsiêu thị", "Tổng SL bán\nđến now", "Tồn đầu kỳ Trung\nBình / ST", "Trung bình SL bán\nđến now / ST", "Tỷ lệ bán / tồn"]
    for index, origin in enumerate(ORIGINS):
        section_row = len(values)
        values.append([origin] + [""] * 7)
        header_row = len(values)
        values.append(labels)
        rows = display_rows(report.base, origin)
        data_start = len(values)
        for row in rows:
            values.append([
                row["region"], row["rsm"], row["stores"], row["stock"], row["sold"],
                row["stock_per_store"], row["sold_per_store"], row["ratio"],
            ])
        sections.append({
            "origin": origin,
            "section_row": section_row,
            "header_row": header_row,
            "data_start": data_start,
            "data_end": len(values),
            "rows": rows,
        })
        if index < len(ORIGINS) - 1:
            values.append([""] * 8)
    # Match the original Excel template: title at D2 and report tables at D4:K.
    # The left/right spacer columns make the exported PDF visually centered.
    return {
        "values": values,
        "sections": sections,
        "title_row": 0,
        "start_row": 1,
        "start_column": 3,
    }


def output_format_requests(
    sheet_id: int, row_count: int, column_count: int, layout: dict[str, Any]
) -> list[dict[str, Any]]:
    used_rows = len(layout["values"])
    start_row = int(layout["start_row"])
    start_column = int(layout["start_column"])
    end_column = start_column + 8
    whole_sheet = grid_range(sheet_id, 0, row_count, 0, column_count)
    report_range = grid_range(sheet_id, start_row, start_row + used_rows, start_column, end_column)
    requests: list[dict[str, Any]] = [
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": sheet_id,
                    "gridProperties": {"rowCount": max(row_count, start_row + used_rows + 2), "columnCount": max(column_count, end_column + 3)},
                },
                "fields": "gridProperties.rowCount,gridProperties.columnCount",
            }
        },
        {"unmergeCells": {"range": whole_sheet}},
        {"repeatCell": {"range": whole_sheet, "cell": {"userEnteredFormat": {}}, "fields": "userEnteredFormat"}},
        {"repeatCell": {"range": report_range, "cell": {"userEnteredFormat": base_cell_format()}, "fields": "userEnteredFormat"}},
        {"updateSheetProperties": {"properties": {"sheetId": sheet_id, "gridProperties": {"hideGridlines": True}}, "fields": "gridProperties.hideGridlines"}},
        {"mergeCells": {"range": grid_range(sheet_id, start_row, start_row + 1, start_column, end_column), "mergeType": "MERGE_ALL"}},
        {"repeatCell": {"range": grid_range(sheet_id, start_row, start_row + 1, start_column, end_column), "cell": {"userEnteredFormat": title_format()}, "fields": "userEnteredFormat"}},
        {"updateDimensionProperties": {"range": dimension_range(sheet_id, "ROWS", start_row, start_row + 1), "properties": {"pixelSize": 34}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {"range": dimension_range(sheet_id, "ROWS", start_row + 1, start_row + used_rows), "properties": {"pixelSize": 24}, "fields": "pixelSize"}},
    ]
    # Same relative column proportions as the workbook shared by the team.
    widths = [105, 185, 74, 145, 132, 152, 168, 106]
    for column, width in enumerate(widths, start=start_column):
        requests.append({"updateDimensionProperties": {"range": dimension_range(sheet_id, "COLUMNS", column, column + 1), "properties": {"pixelSize": width}, "fields": "pixelSize"}})
    for spacer_start, spacer_end in ((0, start_column), (end_column, min(end_column + 3, column_count))):
        if spacer_start < spacer_end:
            requests.append({"updateDimensionProperties": {"range": dimension_range(sheet_id, "COLUMNS", spacer_start, spacer_end), "properties": {"pixelSize": 30}, "fields": "pixelSize"}})
    for section in layout["sections"]:
        color = "#57CC99" if section["origin"] == "NỘI ĐỊA" else "#0096C7"
        section_row = start_row + section["section_row"]
        header_row = start_row + section["header_row"]
        data_start = start_row + section["data_start"]
        data_end = start_row + section["data_end"]
        requests.extend([
            {"mergeCells": {"range": grid_range(sheet_id, section_row, section_row + 1, start_column, end_column), "mergeType": "MERGE_ALL"}},
            {"repeatCell": {"range": grid_range(sheet_id, section_row, section_row + 1, start_column, end_column), "cell": {"userEnteredFormat": section_format(color)}, "fields": "userEnteredFormat"}},
            {"repeatCell": {"range": grid_range(sheet_id, header_row, header_row + 1, start_column, end_column), "cell": {"userEnteredFormat": header_format("#12372A")}, "fields": "userEnteredFormat"}},
            {"repeatCell": {"range": grid_range(sheet_id, header_row, data_end, start_column, end_column), "cell": {"userEnteredFormat": dashed_cell_borders()}, "fields": "userEnteredFormat.borders"}},
            {"updateDimensionProperties": {"range": dimension_range(sheet_id, "ROWS", section_row, section_row + 1), "properties": {"pixelSize": 27}, "fields": "pixelSize"}},
            {"updateDimensionProperties": {"range": dimension_range(sheet_id, "ROWS", header_row, header_row + 1), "properties": {"pixelSize": 43}, "fields": "pixelSize"}},
            {"repeatCell": {"range": grid_range(sheet_id, data_start, data_end, start_column + 1, start_column + 2), "cell": {"userEnteredFormat": {"horizontalAlignment": "LEFT"}}, "fields": "userEnteredFormat.horizontalAlignment"}},
            {"repeatCell": {"range": grid_range(sheet_id, data_start, data_end, start_column + 3, start_column + 7), "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": "#,##0"}}}, "fields": "userEnteredFormat.numberFormat"}},
            {"repeatCell": {"range": grid_range(sheet_id, data_start, data_end, start_column + 7, end_column), "cell": {"userEnteredFormat": {"numberFormat": {"type": "PERCENT", "pattern": "0%"}}}, "fields": "userEnteredFormat.numberFormat"}},
        ])
        for offset, header_color in enumerate(("#12372A", "#12372A", "#1F5F4A", "#23866F", "#23866F", "#4F9A68", "#4F9A68", "#1F5F4A")):
            requests.append({"repeatCell": {"range": grid_range(sheet_id, header_row, header_row + 1, start_column + offset, start_column + offset + 1), "cell": {"userEnteredFormat": header_format(header_color)}, "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment,wrapStrategy)"}})
        for offset, row in enumerate(section["rows"]):
            row_index = data_start + offset
            if row["type"] == "subtotal":
                requests.append({"repeatCell": {"range": grid_range(sheet_id, row_index, row_index + 1, start_column, end_column), "cell": {"userEnteredFormat": total_format("#DCEAF7")}, "fields": "userEnteredFormat(backgroundColor,textFormat)"}})
            elif row["type"] == "grand_total":
                requests.append({"repeatCell": {"range": grid_range(sheet_id, row_index, row_index + 1, start_column, end_column), "cell": {"userEnteredFormat": total_format("#C8E6B8")}, "fields": "userEnteredFormat(backgroundColor,textFormat)"}})
            # Excel used a red -> yellow -> green colour scale on the ratio column,
            # including totals. Rendering the equivalent colour directly keeps PDF/PNG identical.
            requests.append({"repeatCell": {"range": grid_range(sheet_id, row_index, row_index + 1, start_column + 7, end_column), "cell": {"userEnteredFormat": rgb(ratio_color(row["ratio"]))}, "fields": "userEnteredFormat.backgroundColor"}})

        for merge_start, merge_end in region_merge_ranges(section["rows"], data_start):
            requests.append({"mergeCells": {"range": grid_range(sheet_id, merge_start, merge_end, start_column, start_column + 1), "mergeType": "MERGE_ALL"}})
            requests.append({"repeatCell": {"range": grid_range(sheet_id, merge_start, merge_end, start_column, start_column + 1), "cell": {"userEnteredFormat": region_format()}, "fields": "userEnteredFormat.horizontalAlignment,userEnteredFormat.verticalAlignment,userEnteredFormat.textFormat"}})

        # The table body uses dashed horizontal separators; its outline/header use a stronger border.
        requests.extend(outer_table_border_requests(sheet_id, section_row, data_end, start_column, end_column))
    return requests


def grid_range(sheet_id: int, start_row: int, end_row: int, start_column: int, end_column: int) -> dict[str, int]:
    return {"sheetId": sheet_id, "startRowIndex": start_row, "endRowIndex": end_row, "startColumnIndex": start_column, "endColumnIndex": end_column}


def dimension_range(sheet_id: int, dimension: str, start: int, end: int) -> dict[str, Any]:
    return {"sheetId": sheet_id, "dimension": dimension, "startIndex": start, "endIndex": end}


def column_letter(column: int) -> str:
    """Convert a one-indexed column number into its A1 notation counterpart."""
    result = ""
    while column:
        column, remainder = divmod(column - 1, 26)
        result = chr(65 + remainder) + result
    return result


def rgb(hex_color: str) -> dict[str, dict[str, float]]:
    color = hex_color.lstrip("#")
    return {"backgroundColor": {"red": int(color[0:2], 16) / 255, "green": int(color[2:4], 16) / 255, "blue": int(color[4:6], 16) / 255}}


def base_cell_format() -> dict[str, Any]:
    return {"horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE", "wrapStrategy": "WRAP", "textFormat": {"fontFamily": "Arial", "fontSize": 10}}


def title_format() -> dict[str, Any]:
    return {"horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE", "textFormat": {"fontFamily": "Arial", "fontSize": 14, "bold": True, "foregroundColor": {"red": 0.07, "green": 0.15, "blue": 0.12}}}


def section_format(color: str) -> dict[str, Any]:
    return {"horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE", **rgb(color), "textFormat": {"fontFamily": "Arial", "fontSize": 12, "bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}}}


def header_format(color: str) -> dict[str, Any]:
    return {"horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE", "wrapStrategy": "WRAP", **rgb(color), "textFormat": {"fontFamily": "Arial", "fontSize": 10, "bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}}}


def dashed_cell_borders() -> dict[str, Any]:
    vertical = sheet_border("SOLID", "#334155")
    horizontal = sheet_border("DASHED", "#94A3B8")
    return {"borders": {"top": horizontal, "bottom": horizontal, "left": vertical, "right": vertical}}


def sheet_border(style: str, color: str) -> dict[str, Any]:
    value = color.lstrip("#")
    return {
        "style": style,
        "color": {
            "red": int(value[0:2], 16) / 255,
            "green": int(value[2:4], 16) / 255,
            "blue": int(value[4:6], 16) / 255,
        },
    }


def region_format() -> dict[str, Any]:
    return {
        "horizontalAlignment": "CENTER",
        "verticalAlignment": "MIDDLE",
        "textFormat": {"fontFamily": "Arial", "fontSize": 10, "bold": True},
    }


def region_merge_ranges(rows: list[dict[str, Any]], data_start: int) -> list[tuple[int, int]]:
    """Return vertical cell ranges for Miền, ending each merge before a Total row."""
    ranges: list[tuple[int, int]] = []
    active_start: int | None = None
    for index, row in enumerate(rows):
        absolute_row = data_start + index
        if row["type"] == "detail":
            if row["region"]:
                if active_start is not None and absolute_row - active_start > 1:
                    ranges.append((active_start, absolute_row))
                active_start = absolute_row
            continue
        if active_start is not None and absolute_row - active_start > 1:
            ranges.append((active_start, absolute_row))
        active_start = None
    if active_start is not None and data_start + len(rows) - active_start > 1:
        ranges.append((active_start, data_start + len(rows)))
    return ranges


def outer_table_border_requests(
    sheet_id: int, section_row: int, data_end: int, start_column: int, end_column: int
) -> list[dict[str, Any]]:
    medium = sheet_border("SOLID_MEDIUM", "#111111")
    solid = sheet_border("SOLID", "#111111")
    header_row = section_row + 1
    return [
        {"repeatCell": {"range": grid_range(sheet_id, section_row, section_row + 1, start_column, end_column), "cell": {"userEnteredFormat": {"borders": {"top": medium, "bottom": solid}}}, "fields": "userEnteredFormat.borders.top,userEnteredFormat.borders.bottom"}},
        {"repeatCell": {"range": grid_range(sheet_id, header_row, header_row + 1, start_column, end_column), "cell": {"userEnteredFormat": {"borders": {"top": solid, "bottom": solid}}}, "fields": "userEnteredFormat.borders.top,userEnteredFormat.borders.bottom"}},
        {"repeatCell": {"range": grid_range(sheet_id, section_row, data_end, start_column, start_column + 1), "cell": {"userEnteredFormat": {"borders": {"left": medium}}}, "fields": "userEnteredFormat.borders.left"}},
        {"repeatCell": {"range": grid_range(sheet_id, section_row, data_end, end_column - 1, end_column), "cell": {"userEnteredFormat": {"borders": {"right": medium}}}, "fields": "userEnteredFormat.borders.right"}},
        {"repeatCell": {"range": grid_range(sheet_id, data_end - 1, data_end, start_column, end_column), "cell": {"userEnteredFormat": {"borders": {"bottom": medium}}}, "fields": "userEnteredFormat.borders.bottom"}},
    ]


def total_format(color: str) -> dict[str, Any]:
    return {**rgb(color), "textFormat": {"fontFamily": "Arial", "fontSize": 10, "bold": True, "foregroundColor": {"red": 0.78, "green": 0.12, "blue": 0.12}}}


def output_sheet_name() -> str:
    return os.getenv("GOOGLE_SHEET_OUTPUT_TAB", OUTPUT_SHEET_DEFAULT).strip() or OUTPUT_SHEET_DEFAULT


def export_output_sheet_pdf(spreadsheet_id: str, sheet_id: int) -> bytes:
    credentials = service_account_credentials(write=True)
    credentials.refresh(GoogleRequest())
    response = httpx.get(
        f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export",
        params={"format": "pdf", "gid": str(sheet_id), "single": "true", "size": "A3", "portrait": "false", "fitw": "true", "scale": "4", "gridlines": "false", "sheetnames": "false", "pagenumbers": "false", "horizontal_alignment": "CENTER", "vertical_alignment": "TOP", "top_margin": "0.15", "bottom_margin": "0.15", "left_margin": "0.15", "right_margin": "0.15"},
        headers={"Authorization": f"Bearer {credentials.token}"},
        timeout=60,
        follow_redirects=True,
    )
    if response.is_error or not response.content:
        raise ReportDataError(f"Google không export được tab {output_sheet_name()} (HTTP {response.status_code}).")
    return response.content


def render_pdf_to_png(pdf_bytes: bytes, output_path: Path) -> None:
    document = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        if document.page_count != 1:
            raise ReportDataError("Tab output_tri_an bị export thành nhiều trang; cần thu gọn bố cục.")
        page = document.load_page(0)
        pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        pixmap.save(str(output_path))
    finally:
        document.close()


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
    # Same three-stop scale as the original openpyxl report:
    # red at 0%, yellow at 4%, and green at 12% or above.
    stops = ((0.00, "#F8696B"), (0.04, "#FFEB84"), (0.12, "#63BE7B"))
    bounded = min(max(float(ratio), stops[0][0]), stops[-1][0])
    for (low_value, low_color), (high_value, high_color) in zip(stops, stops[1:]):
        if bounded <= high_value:
            fraction = (bounded - low_value) / (high_value - low_value)
            low = tuple(int(low_color[index:index + 2], 16) for index in (1, 3, 5))
            high = tuple(int(high_color[index:index + 2], 16) for index in (1, 3, 5))
            return "#" + "".join(f"{round(a + (b - a) * fraction):02X}" for a, b in zip(low, high))
    return stops[-1][1]


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
