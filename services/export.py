"""Excel and PDF report generators with unique filenames and async disk writes."""

from __future__ import annotations

import asyncio
import datetime
import re
import uuid
from pathlib import Path

from fpdf import FPDF
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill

from config import BOT_TZ, EXPORT_DIR
from database import crud
from database.models import (
    ALL_METERS,
    ELECTRICITY_METERS,
    GAS_METERS,
    METER_PLAIN_LABELS,
    METER_UNITS,
    WATER_METERS,
    format_property_short,
    format_property_title,
)
from services.calculations import calculate_billing, fmt_date

FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
EXCEL_HISTORY_LIMIT = 500
PDF_HISTORY_LIMIT = 40


def _fmt_opt(val) -> str:
    return "—" if val is None else str(val)


def _safe_sheet_title(raw: str, used: set[str]) -> str:
    cleaned = re.sub(r"[\\/*?:\[\]]", "_", raw).strip() or "Объект"
    base = cleaned[:31]
    if base not in used:
        used.add(base)
        return base
    idx = 2
    while True:
        suffix = f" ({idx})"
        candidate = f"{cleaned[:31 - len(suffix)]}{suffix}"
        if candidate not in used:
            used.add(candidate)
            return candidate
        idx += 1


async def generate_excel_report(apartments: list) -> Path:
    wb = Workbook()
    first = True
    used_titles: set[str] = set()
    header_fill = PatternFill(start_color="2563EB", end_color="2563EB", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True)
    bold_font = Font(bold=True)

    for apt in apartments:
        title = _safe_sheet_title(format_property_short(apt), used_titles)
        ws = wb.active if first else wb.create_sheet(title=title)
        ws.title = title
        first = False

        ws.append([
            format_property_title(apt),
            f"Договор: {apt.agreement_no or '—'}",
            f"Срок: {fmt_date(apt.agreement_date_start)} — {fmt_date(apt.agreement_date_finish)}",
        ])
        ws["A1"].font = bold_font
        ws.append([])

        billing = await calculate_billing(apt.id)
        ws.append(["Текущий расчёт", billing.period_label])
        ws[f"A{ws.max_row}"].font = bold_font
        ws.append(["Счётчик", "Предыдущее", "Текущее", "Расход", "Ед.", "Тариф (₽)", "Сумма (₽)"])
        for cell in ws[ws.max_row]:
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center")

        for mt in ALL_METERS:
            m = billing.meters.get(mt)
            if m:
                ws.append([
                    METER_PLAIN_LABELS[mt],
                    _fmt_opt(m.previous),
                    _fmt_opt(m.current) if m.submitted else "Не подано",
                    m.consumption,
                    METER_UNITS[mt],
                    float(m.tariff) if m.tariff is not None else "Не задан",
                    m.cost if (m.has_base and m.has_tariff and m.submitted) else "—",
                ])

        if any(m in billing.meters for m in WATER_METERS):
            ws.append(["Итого вода", "", "", billing.water_consumption_total, "м³", "", billing.water_total])
        if any(m in billing.meters for m in ELECTRICITY_METERS):
            ws.append(["Итого электричество", "", "", billing.electricity_consumption_total, "кВт·ч", "", billing.electricity_total])
        if any(m in billing.meters for m in GAS_METERS):
            ws.append(["Итого газ", "", "", billing.gas_consumption_total, "м³", "", billing.gas_total])
        ws.append(["ВСЕГО К ОПЛАТЕ", "", "", "", "", "", billing.grand_total])
        ws[f"A{ws.max_row}"].font = bold_font
        ws[f"G{ws.max_row}"].font = bold_font
        ws.append([])

        total_count = await crud.get_readings_count(apt.id)
        readings = await crud.get_all_readings(apt.id, limit=EXCEL_HISTORY_LIMIT)
        hist_title = (
            f"История показаний (показаны последние {len(readings)} из {total_count})"
            if total_count > len(readings)
            else f"История показаний (всего {total_count})"
        )
        ws.append([hist_title])
        ws[f"A{ws.max_row}"].font = bold_font
        ws.append(["ID", "Дата", "Счётчик", "Значение", "Ед.", "Кем подано (TG ID)"])
        for cell in ws[ws.max_row]:
            cell.fill = header_fill
            cell.font = header_font

        for r in readings:
            ws.append([
                r.id,
                r.reading_date.strftime("%d.%m.%Y"),
                METER_PLAIN_LABELS.get(r.meter_type, r.meter_type),
                r.value,
                METER_UNITS.get(r.meter_type, ""),
                r.submitted_by,
            ])

        for col in ws.columns:
            max_len = max(len(str(c.value or "")) for c in col)
            ws.column_dimensions[col[0].column_letter].width = min(max_len + 4, 34)

    ts = datetime.datetime.now(BOT_TZ).strftime("%Y%m%d_%H%M%S")
    filepath = EXPORT_DIR / f"utility_report_{ts}_{uuid.uuid4().hex[:8]}.xlsx"
    await asyncio.to_thread(wb.save, filepath)
    return filepath


async def generate_pdf_report(apartments: list) -> Path:
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=14)
    pdf.add_font("DejaVu", "", FONT_PATH)
    pdf.add_font("DejaVu", "B", FONT_BOLD_PATH)

    for apt in apartments:
        pdf.add_page()
        pdf.set_font("DejaVu", "B", 14)
        pdf.cell(0, 10, f"Отчёт — {format_property_title(apt)}", new_x="LMARGIN", new_y="NEXT")

        pdf.set_font("DejaVu", "", 10)
        pdf.cell(
            0, 6,
            f"Договор: {apt.agreement_no or '—'} | Период: {fmt_date(apt.agreement_date_start)} — {fmt_date(apt.agreement_date_finish)}",
            new_x="LMARGIN", new_y="NEXT",
        )
        pdf.ln(3)

        billing = await calculate_billing(apt.id)
        pdf.set_font("DejaVu", "B", 11)
        pdf.cell(0, 8, f"Текущий расчёт ({billing.period_label})", new_x="LMARGIN", new_y="NEXT")

        headers = ["Счётчик", "Пред.", "Тек.", "Расход", "Тариф", "Сумма (₽)"]
        widths = [32, 26, 26, 28, 36, 36]
        pdf.set_font("DejaVu", "B", 9)
        for h_txt, w in zip(headers, widths):
            pdf.cell(w, 7, h_txt, border=1, align="C")
        pdf.ln()

        pdf.set_font("DejaVu", "", 9)
        for mt in ALL_METERS:
            m = billing.meters.get(mt)
            if not m:
                continue
            row = [
                f"{METER_PLAIN_LABELS[mt]} ({METER_UNITS[mt]})",
                _fmt_opt(m.previous),
                _fmt_opt(m.current) if m.submitted else "—",
                str(m.consumption),
                f"{float(m.tariff):.2f}" if m.tariff is not None else "—",
                str(m.cost) if (m.has_base and m.has_tariff and m.submitted) else "—",
            ]
            for val, w in zip(row, widths):
                pdf.cell(w, 7, val, border=1, align="C")
            pdf.ln()

        totals_parts = []
        if any(m in billing.meters for m in WATER_METERS):
            totals_parts.append(f"Вода: {billing.water_total} ₽")
        if any(m in billing.meters for m in ELECTRICITY_METERS):
            totals_parts.append(f"Электричество: {billing.electricity_total} ₽")
        if any(m in billing.meters for m in GAS_METERS):
            totals_parts.append(f"Газ: {billing.gas_total} ₽")
        totals_parts.append(f"ИТОГО: {billing.grand_total} ₽")

        pdf.set_font("DejaVu", "B", 10)
        pdf.ln(2)
        pdf.cell(0, 7, "  |  ".join(totals_parts), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(4)

        total_count = await crud.get_readings_count(apt.id)
        readings = await crud.get_all_readings(apt.id, limit=PDF_HISTORY_LIMIT)
        hist_title = (
            f"Последние показания (показано {len(readings)} из {total_count})"
            if total_count > len(readings)
            else f"История показаний (всего {total_count})"
        )
        pdf.set_font("DejaVu", "B", 11)
        pdf.cell(0, 8, hist_title, new_x="LMARGIN", new_y="NEXT")
        if not readings:
            pdf.set_font("DejaVu", "", 9)
            pdf.cell(0, 6, "Показаний пока нет.", new_x="LMARGIN", new_y="NEXT")
        else:
            r_headers = ["Дата", "Счётчик", "Значение", "TG ID"]
            r_widths = [36, 44, 44, 60]
            pdf.set_font("DejaVu", "B", 9)
            for h_txt, w in zip(r_headers, r_widths):
                pdf.cell(w, 7, h_txt, border=1, align="C")
            pdf.ln()
            pdf.set_font("DejaVu", "", 9)
            for r in readings:
                row = [
                    r.reading_date.strftime("%d.%m.%Y"),
                    METER_PLAIN_LABELS.get(r.meter_type, r.meter_type),
                    f"{r.value} {METER_UNITS.get(r.meter_type, '')}",
                    str(r.submitted_by),
                ]
                for val, w in zip(row, r_widths):
                    pdf.cell(w, 6, val, border=1, align="C")
                pdf.ln()

    ts = datetime.datetime.now(BOT_TZ).strftime("%Y%m%d_%H%M%S")
    filepath = EXPORT_DIR / f"utility_report_{ts}_{uuid.uuid4().hex[:8]}.pdf"
    await asyncio.to_thread(pdf.output, str(filepath))
    return filepath
