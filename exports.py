from datetime import datetime
from pathlib import Path
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from fpdf import FPDF
from sqlalchemy import desc, select

from config import EXPORT_DIR, TZ
from database import SessionLocal
from models import Apartment, MeterReading
from ui import fmt_dec


async def generate_excel_report(apartment_id: int | None = None, family_id: int | None = None) -> Path:
    wb = Workbook()
    wb.remove(wb.active)

    async with SessionLocal() as session:
        if apartment_id:
            apt_obj = await session.get(Apartment, apartment_id)
            apts = [apt_obj] if (apt_obj and (family_id is None or apt_obj.family_id == family_id)) else []
        else:
            stmt = select(Apartment).order_by(Apartment.number)
            if family_id is not None:
                stmt = stmt.where(Apartment.family_id == family_id)
            apts = list((await session.execute(stmt)).scalars().all())

        for apt in apts:
            if not apt:
                continue
            ws = wb.create_sheet(title=f"Кв_{apt.number}"[:31])
            header_fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
            header_font = Font(color="FFFFFF", bold=True)

            ws.append([f"Отчёт по коммунальным ресурсам — Квартира №{apt.number}"])
            ws["A1"].font = Font(size=13, bold=True)
            ws.append(
                [
                    f"Договор: №{apt.agreement_no or '—'} | Действующие тарифы: "
                    f"ХВС={apt.tariff_hvs} ₽, ГВС={apt.tariff_gvs} ₽, Т1={apt.tariff_t1} ₽, Т2={apt.tariff_t2} ₽, Т3={apt.tariff_t3} ₽"
                ]
            )
            ws.append([])

            headers = [
                "Период",
                "Дата подачи",
                "Telegram ID",
                "ХВС нач.",
                "ХВС кон.",
                "Расход ХВС (м³)",
                "Сумма ХВС (₽)",
                "ГВС нач.",
                "ГВС кон.",
                "Расход ГВС (м³)",
                "Сумма ГВС (₽)",
                "Итого Вода (₽)",
                "Т1 нач.",
                "Т1 кон.",
                "Расход Т1",
                "Сумма Т1 (₽)",
                "Т2 нач.",
                "Т2 кон.",
                "Расход Т2",
                "Сумма Т2 (₽)",
                "Т3 нач.",
                "Т3 кон.",
                "Расход Т3",
                "Сумма Т3 (₽)",
                "Итого Свет (₽)",
                "ОБЩАЯ СУММА (₽)",
            ]
            ws.append(headers)
            for col_num in range(1, len(headers) + 1):
                cell = ws.cell(row=4, column=col_num)
                cell.fill = header_fill
                cell.font = header_font
                cell.alignment = Alignment(horizontal="center")

            readings = (
                await session.execute(
                    select(MeterReading)
                    .where(MeterReading.apartment_id == apt.id)
                    .order_by(desc(MeterReading.period))
                )
            ).scalars().all()

            for r in readings:
                ws.append(
                    [
                        r.period,
                        r.reading_date.strftime("%d.%m.%Y"),
                        r.submitted_by,
                        int(round(float(r.prev_hvs))),
                        int(round(float(r.hvs))),
                        int(round(float(r.diff_hvs))),
                        int(r.cost_hvs),
                        int(round(float(r.prev_gvs))),
                        int(round(float(r.gvs))),
                        int(round(float(r.diff_gvs))),
                        int(r.cost_gvs),
                        int(r.cost_water_total),
                        int(round(float(r.prev_t1))),
                        int(round(float(r.t1))),
                        int(round(float(r.diff_t1))),
                        int(r.cost_t1),
                        int(round(float(r.prev_t2))),
                        int(round(float(r.t2))),
                        int(round(float(r.diff_t2))),
                        int(r.cost_t2),
                        int(round(float(r.prev_t3))),
                        int(round(float(r.t3))),
                        int(round(float(r.diff_t3))),
                        int(r.cost_t3),
                        int(r.cost_elec_total),
                        int(r.cost_total),
                    ]
                )

            for col in ws.columns:
                max_len = max(len(str(cell.value or "")) for cell in col)
                ws.column_dimensions[col[0].column_letter].width = min(max(max_len + 3, 11), 35)

    if not wb.sheetnames:
        wb.create_sheet(title="Пусто")

    ts = datetime.now(TZ).strftime("%Y%m%d_%H%M%S")
    scope = f"apt_{apartment_id}" if apartment_id else f"fam_{family_id or 'all'}"
    file_path = EXPORT_DIR / f"utility_report_{scope}_{ts}.xlsx"
    wb.save(str(file_path))
    return file_path


def _find_cyrillic_font() -> str | None:
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    return None


async def generate_pdf_report(apartment_id: int | None = None, family_id: int | None = None) -> Path:
    pdf = FPDF(orientation="L", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=12)

    font_path = _find_cyrillic_font()
    if font_path:
        pdf.add_font("CustomSans", "", font_path)
        font_family = "CustomSans"
    else:
        font_family = "Helvetica"

    async with SessionLocal() as session:
        if apartment_id:
            apt_obj = await session.get(Apartment, apartment_id)
            apts = [apt_obj] if (apt_obj and (family_id is None or apt_obj.family_id == family_id)) else []
        else:
            stmt = select(Apartment).order_by(Apartment.number)
            if family_id is not None:
                stmt = stmt.where(Apartment.family_id == family_id)
            apts = list((await session.execute(stmt)).scalars().all())

        if not apts:
            pdf.add_page()
            pdf.set_font(font_family, size=12)
            pdf.cell(0, 10, "Нет данных по квартирам", new_x="LMARGIN", new_y="NEXT")

        for apt in apts:
            if not apt:
                continue
            pdf.add_page()
            pdf.set_font(font_family, size=14)
            pdf.cell(0, 10, f"Отчёт по коммунальным ресурсам — Квартира №{apt.number}", new_x="LMARGIN", new_y="NEXT")
            pdf.set_font(font_family, size=9)
            pdf.cell(
                0,
                7,
                f"Договор: №{apt.agreement_no or '—'} | Тарифы: ХВС={apt.tariff_hvs} руб, ГВС={apt.tariff_gvs} руб, "
                f"Т1={apt.tariff_t1}, Т2={apt.tariff_t2}, Т3={apt.tariff_t3} руб/кВт*ч",
                new_x="LMARGIN",
                new_y="NEXT",
            )
            pdf.ln(3)

            cols = [
                ("Период", 22),
                ("Дата", 24),
                ("ХВС расх.", 25),
                ("ГВС расх.", 25),
                ("Вода (руб)", 26),
                ("Т1 расх.", 25),
                ("Т2 расх.", 25),
                ("Т3 расх.", 25),
                ("Свет (руб)", 26),
                ("ИТОГО (руб)", 32),
            ]
            for title, w in cols:
                pdf.cell(w, 8, title, border=1, align="C")
            pdf.ln()

            readings = (
                await session.execute(
                    select(MeterReading)
                    .where(MeterReading.apartment_id == apt.id)
                    .order_by(desc(MeterReading.period))
                )
            ).scalars().all()

            for r in readings:
                row_vals = [
                    r.period,
                    r.reading_date.strftime("%d.%m.%Y"),
                    f"{fmt_dec(r.diff_hvs, 0)} м3",
                    f"{fmt_dec(r.diff_gvs, 0)} м3",
                    f"{fmt_dec(r.cost_water_total, 0)}",
                    f"{fmt_dec(r.diff_t1, 0)}",
                    f"{fmt_dec(r.diff_t2, 0)}",
                    f"{fmt_dec(r.diff_t3, 0)}",
                    f"{fmt_dec(r.cost_elec_total, 0)}",
                    f"{fmt_dec(r.cost_total, 0)} руб.",
                ]
                for (_, w), val in zip(cols, row_vals):
                    pdf.cell(w, 7, val, border=1, align="C")
                pdf.ln()

    ts = datetime.now(TZ).strftime("%Y%m%d_%H%M%S")
    scope = f"apt_{apartment_id}" if apartment_id else f"fam_{family_id or 'all'}"
    file_path = EXPORT_DIR / f"utility_report_{scope}_{ts}.pdf"
    pdf.output(str(file_path))
    return file_path
