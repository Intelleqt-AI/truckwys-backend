"""
PDF Generator service for creating professional invoice PDFs.

Uses ReportLab to generate South African Tax Invoice PDFs compliant with
VAT requirements.
"""

from decimal import Decimal
from typing import Optional
from datetime import date
import os
from io import BytesIO

from django.conf import settings
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image
)
from reportlab.lib.enums import TA_LEFT, TA_RIGHT, TA_CENTER

from core.models import Invoice, Company


class InvoicePDFGenerator:
    """Service for generating invoice PDFs."""

    def __init__(self, invoice: Invoice):
        """
        Initialize PDF generator.

        Args:
            invoice: Invoice to generate PDF for
        """
        self.invoice = invoice
        self.buffer = BytesIO()
        self.width, self.height = A4
        self.styles = getSampleStyleSheet()
        self._setup_custom_styles()

    def _setup_custom_styles(self) -> None:
        """Set up custom paragraph styles."""
        self.styles.add(ParagraphStyle(
            name='InvoiceTitle',
            parent=self.styles['Heading1'],
            fontSize=24,
            textColor=colors.HexColor('#1e3a8a'),
            spaceAfter=12,
            alignment=TA_CENTER,
        ))

        self.styles.add(ParagraphStyle(
            name='CompanyName',
            parent=self.styles['Normal'],
            fontSize=16,
            textColor=colors.HexColor('#1e3a8a'),
            fontName='Helvetica-Bold',
            spaceAfter=6,
        ))

        self.styles.add(ParagraphStyle(
            name='SectionHeader',
            parent=self.styles['Normal'],
            fontSize=10,
            textColor=colors.HexColor('#64748b'),
            fontName='Helvetica-Bold',
            spaceAfter=4,
        ))

        self.styles.add(ParagraphStyle(
            name='RightAlign',
            parent=self.styles['Normal'],
            alignment=TA_RIGHT,
        ))

    def generate(self) -> str:
        """
        Generate invoice PDF and save to file.

        Returns:
            str: Path to the saved PDF file
        """
        # Create document
        doc = SimpleDocTemplate(
            self.buffer,
            pagesize=A4,
            rightMargin=20*mm,
            leftMargin=20*mm,
            topMargin=20*mm,
            bottomMargin=20*mm,
        )

        # Build content
        story = []
        story.extend(self._build_header())
        story.append(Spacer(1, 10*mm))
        story.extend(self._build_invoice_info())
        story.append(Spacer(1, 10*mm))
        story.extend(self._build_customer_info())
        story.append(Spacer(1, 10*mm))
        story.extend(self._build_line_items())
        story.append(Spacer(1, 10*mm))
        story.extend(self._build_totals())
        story.append(Spacer(1, 10*mm))
        story.extend(self._build_footer())

        # Build PDF
        doc.build(story)

        # Save to file
        return self._save_to_file()

    def _build_header(self) -> list:
        """Build PDF header with company logo and details."""
        elements = []

        # Get company details
        try:
            company = Company.objects.first()
        except Company.DoesNotExist:
            company = None

        # Company name
        company_name = company.company_name if company else "TruckWys"
        elements.append(Paragraph(company_name, self.styles['CompanyName']))

        # Company details
        if company:
            company_info = []
            if company.registration_number:
                company_info.append(f"Reg No: {company.registration_number}")
            if company.vat_number:
                company_info.append(f"VAT No: {company.vat_number}")

            if company_info:
                elements.append(Paragraph(" | ".join(company_info), self.styles['Normal']))

            # Address
            if company.address:
                address_lines = []
                if isinstance(company.address, dict):
                    if company.address.get('street'):
                        address_lines.append(company.address['street'])
                    city_parts = []
                    if company.address.get('city'):
                        city_parts.append(company.address['city'])
                    if company.address.get('postal_code'):
                        city_parts.append(company.address['postal_code'])
                    if city_parts:
                        address_lines.append(", ".join(city_parts))

                if address_lines:
                    elements.append(Paragraph("<br/>".join(address_lines), self.styles['Normal']))

            # Contact
            if company.contact:
                contact_info = []
                if isinstance(company.contact, dict):
                    if company.contact.get('phone'):
                        contact_info.append(f"Tel: {company.contact['phone']}")
                    if company.contact.get('email'):
                        contact_info.append(f"Email: {company.contact['email']}")

                if contact_info:
                    elements.append(Paragraph(" | ".join(contact_info), self.styles['Normal']))

        elements.append(Spacer(1, 5*mm))

        # TAX INVOICE title
        elements.append(Paragraph("TAX INVOICE", self.styles['InvoiceTitle']))

        return elements

    def _build_invoice_info(self) -> list:
        """Build invoice information section."""
        elements = []

        data = [
            ['Invoice Number:', self.invoice.invoice_number],
            ['Invoice Date:', self.invoice.issue_date.strftime('%d %B %Y')],
            ['Due Date:', self.invoice.due_date.strftime('%d %B %Y')],
            ['Payment Terms:', self.invoice.get_payment_terms_display()],
        ]

        table = Table(data, colWidths=[40*mm, 60*mm])
        table.setStyle(TableStyle([
            ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 10),
            ('TEXTCOLOR', (0, 0), (0, -1), colors.HexColor('#64748b')),
            ('ALIGN', (0, 0), (0, -1), 'LEFT'),
            ('ALIGN', (1, 0), (1, -1), 'LEFT'),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ]))

        elements.append(table)
        return elements

    def _build_customer_info(self) -> list:
        """Build customer information section."""
        elements = []

        elements.append(Paragraph("BILL TO:", self.styles['SectionHeader']))

        customer = self.invoice.customer
        customer_lines = [f"<b>{customer.name}</b>"]

        if hasattr(customer, 'vat_number') and customer.vat_number:
            customer_lines.append(f"VAT No: {customer.vat_number}")

        if customer.billing_address:
            customer_lines.append(customer.billing_address)
        elif customer.address:
            # Fall back to regular address if billing address not set
            address_parts = [customer.address, customer.city, customer.state, customer.zip_code]
            address_str = ", ".join(filter(None, address_parts))
            if address_str:
                customer_lines.append(address_str)

        if customer.email:
            customer_lines.append(f"Email: {customer.email}")

        if customer.phone:
            customer_lines.append(f"Phone: {customer.phone}")

        elements.append(Paragraph("<br/>".join(customer_lines), self.styles['Normal']))

        return elements

    def _build_line_items(self) -> list:
        """Build line items table."""
        elements = []

        elements.append(Paragraph("ITEMS:", self.styles['SectionHeader']))
        elements.append(Spacer(1, 3*mm))

        # Header row
        data = [['Description', 'Quantity', 'Unit Price', 'Amount']]

        # Line items
        line_items = self.invoice.line_items or []
        for item in line_items:
            data.append([
                item.get('description', ''),
                str(item.get('quantity', 1)),
                f"R {item.get('unit_price', 0):,.2f}",
                f"R {item.get('amount', 0):,.2f}",
            ])

        # If no line items, show basic freight charge
        if not line_items:
            data.append([
                f"Freight Charge - {self.invoice.load.pickup_location + ' → ' + self.invoice.load.delivery_location if self.invoice.load else 'Service'}",
                '1',
                f"R {self.invoice.subtotal:,.2f}",
                f"R {self.invoice.subtotal:,.2f}",
            ])

        table = Table(data, colWidths=[90*mm, 25*mm, 30*mm, 30*mm])
        table.setStyle(TableStyle([
            # Header styling
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1e3a8a')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 10),
            ('ALIGN', (0, 0), (0, 0), 'LEFT'),
            ('ALIGN', (1, 0), (-1, 0), 'RIGHT'),
            # Body styling
            ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'),
            ('FONTSIZE', (0, 1), (-1, -1), 9),
            ('ALIGN', (0, 1), (0, -1), 'LEFT'),
            ('ALIGN', (1, 1), (-1, -1), 'RIGHT'),
            # Grid
            ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#e2e8f0')),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            # Padding
            ('TOPPADDING', (0, 0), (-1, -1), 6),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
            ('LEFTPADDING', (0, 0), (-1, -1), 8),
            ('RIGHTPADDING', (0, 0), (-1, -1), 8),
        ]))

        elements.append(table)
        return elements

    def _build_totals(self) -> list:
        """Build totals section."""
        elements = []

        data = [
            ['Subtotal:', f"R {self.invoice.subtotal:,.2f}"],
            ['VAT (15%):', f"R {self.invoice.vat_amount:,.2f}"],
        ]

        if self.invoice.discount > 0:
            data.append(['Discount:', f"R -{self.invoice.discount:,.2f}"])

        data.append(['<b>TOTAL:</b>', f"<b>R {self.invoice.total_amount:,.2f}</b>"])

        if self.invoice.paid_amount > 0:
            data.append(['Paid:', f"R {self.invoice.paid_amount:,.2f}"])
            data.append(['<b>Balance Due:</b>', f"<b>R {self.invoice.balance:,.2f}</b>"])

        table = Table(data, colWidths=[140*mm, 35*mm])
        table.setStyle(TableStyle([
            ('FONTNAME', (0, 0), (0, -1), 'Helvetica'),
            ('FONTSIZE', (0, 0), (-1, -1), 10),
            ('ALIGN', (0, 0), (0, -1), 'RIGHT'),
            ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
            # Last row (total) styling
            ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
            ('FONTSIZE', (0, -1), (-1, -1), 12),
            ('LINEABOVE', (0, -1), (-1, -1), 1.5, colors.HexColor('#1e3a8a')),
            ('TOPPADDING', (0, -1), (-1, -1), 8),
        ]))

        elements.append(table)
        return elements

    def _build_footer(self) -> list:
        """Build PDF footer with payment details."""
        elements = []

        # Payment terms
        if self.invoice.notes:
            elements.append(Paragraph("<b>Notes:</b>", self.styles['SectionHeader']))
            elements.append(Paragraph(self.invoice.notes, self.styles['Normal']))
            elements.append(Spacer(1, 5*mm))

        # Banking details
        elements.append(Paragraph("<b>BANKING DETAILS:</b>", self.styles['SectionHeader']))
        banking_info = [
            "Bank: First National Bank (FNB)",
            "Account Name: TruckWys (Pty) Ltd",
            "Account Number: 62 XXXX XXXX",
            "Branch Code: 250 655",
            "Swift Code: FIRNZAJJ",
        ]
        elements.append(Paragraph("<br/>".join(banking_info), self.styles['Normal']))
        elements.append(Spacer(1, 5*mm))

        # Footer text
        footer_text = "Please use the invoice number as payment reference. Thank you for your business!"
        elements.append(Paragraph(footer_text, self.styles['Normal']))

        return elements

    def _save_to_file(self) -> str:
        """
        Save the PDF buffer to a file.

        Returns:
            str: Relative path to the saved file
        """
        # Create directory if it doesn't exist
        invoice_dir = os.path.join(
            settings.MEDIA_ROOT,
            'invoices',
            str(self.invoice.issue_date.year),
            str(self.invoice.issue_date.month).zfill(2)
        )
        os.makedirs(invoice_dir, exist_ok=True)

        # Generate filename
        filename = f"{self.invoice.invoice_number}.pdf"
        filepath = os.path.join(invoice_dir, filename)

        # Write buffer to file
        with open(filepath, 'wb') as f:
            f.write(self.buffer.getvalue())

        # Return relative path
        relative_path = os.path.join(
            'invoices',
            str(self.invoice.issue_date.year),
            str(self.invoice.issue_date.month).zfill(2),
            filename
        )

        return relative_path

    @classmethod
    def generate_pdf(cls, invoice: Invoice) -> str:
        """
        Convenience method to generate and save PDF for an invoice.

        Args:
            invoice: Invoice to generate PDF for

        Returns:
            str: Path to the saved PDF file
        """
        generator = cls(invoice)
        return generator.generate()
