"""Excel and PDF report generators for apartment readings and billing."""

from __future__ import annotations

import datetime
from pathlib import Path

from fpdf import FPDF
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill

from config import EXPORT_DIR
from database import crud
from database.models import ALL_METERS, METER_PLAIN_LABELS, METER_UNITS
from services.calculations import calculate_billing, fmt_date

FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


async def generate_excel_report(apartments: list) -> Path:
    wb = Workbook()
    first = True
    header_fill = PatternFill(start_color="2563EB", end_color="2563EB", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True)
    bold_font = Font(bold=True)

    for apt in apartments:
        title = f"Кв_{apt.number}"[:31]
        ws = wb.active if first else wb.create_sheet(title=title)
        ws.title = title
        first = False

        ws.append([
            f"Квартира №{apt.number}",
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
                    m.previous,
                    m.current,
                    m.consumption,
                    METER_UNITS[mt],
                    float(m.tariff),
                    m.cost,
                ])

        ws.append(["Итого вода", "", "", billing.water_consumption_total, "м³", "", billing.water_total])
        ws.append(["Итого электричество", "", "", billing.electricity_consumption_total, "кВт·ч", "", billing.electricity_total])
        ws.append(["ВСЕГО К ОПЛАТЕ", "", "", "", "", "", billing.grand_total])
        ws[f"A{ws.max_row}"].font = bold_font
        ws[f"G{ws.max_row}"].font = bold_font
        ws.append([])

        ws.append(["История показаний"])
        ws[f"A{ws.max_row}"].font = bold_font
        ws.append(["ID", "Дата", "Счётчик", "Значение", "Ед.", "Кем подано (TG ID)"])
        for cell in ws[ws.max_row]:
            cell.fill = header_fill
            cell.font = header_font

        readings = await crud.get_all_readings(apt.id, limit=500)
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

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = EXPORT_DIR / f"utility_report_{ts}.xlsx"
    wb.save(filepath)
    return filepath


async def generate_pdf_report(apartments: list) -> Path:
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=14)
    pdf.add_font("DejaVu", "", FONT_PATH)
    pdf.add_font("DejaVu", "B", FONT_BOLD_PATH)

    for apt in apartments:
        pdf.add_page()
        pdf.set_font("DejaVu", "B", 14)
        pdf.cell(0, 10, f"Отчёт по квартире №{apt.number}", new_x="LMARGIN", new_y="NEXT")

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
        for h, w in zip(headers, widths):
            pdf.cell(w, 7, h, border=1, align="C")
        pdf.ln()

        pdf.set_font("DejaVu", "", 9)
        for mt in ALL_METERS:
            m = billing.meters.get(mt)
            if not m:
                continue
            row = [
                f"{METER_PLAIN_LABELS[mt]} ({METER_UNITS[mt]})",
                str(m.previous),
                str(m.current),
                str(m.consumption),
                f"{float(m.tariff):.2f}",
                str(m.cost),
            ]
            for val, w in zip(row, widths):
                pdf.cell(w, 7, val, border=1, align="C")
            pdf.ln()

        pdf.set_font("DejaVu", "B", 10)
        pdf.ln(2)
        pdf.cell(
            0, 7,
            f"Вода: {billing.water_total} ₽  |  Электричество: {billing.electricity_total} ₽  |  ИТОГО: {billing.grand_total} ₽",
            new_x="LMARGIN", new_y="NEXT",
        )
        pdf.ln(4)

        pdf.set_font("DejaVu", "B", 11)
        pdf.cell(0, 8, "Последние показания", new_x="LMARGIN", new_y="NEXT")
        readings = await crud.get_all_readings(apt.id, limit=40)
        if not readings:
            pdf.set_font("DejaVu", "", 9)
            pdf.cell(0, 6, "Показаний пока нет.", new_x="LMARGIN", new_y="NEXT")
        else:
            r_headers = ["Дата", "Счётчик", "Значение", "TG ID"]
            r_widths = [36, 44, 44, 60]
            pdf.set_font("DejaVu", "B", 9)
            for h, w in zip(r_headers, r_widths):
                pdf.cell(w, 7, h, border=1, align="C")
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

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = EXPORT_DIR / f"utility_report_{ts}.pdf"
    pdf.output(str(filepath))
    return filepath
