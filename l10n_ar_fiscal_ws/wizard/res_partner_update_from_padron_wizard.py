import logging
from ast import literal_eval

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class ResPartnerUpdateFromPadronField(models.TransientModel):
    _name = "res.partner.update.from.padron.field"
    _description = "ARCA A5 Census Field"

    wizard_id = fields.Many2one(
        "res.partner.update.from.padron.wizard",
        "Wizard",
    )
    field = fields.Char()
    old_value = fields.Char()
    new_value = fields.Char()
    real_value = fields.Char(help="Stores the actual value (ID for Many2one, " "list for Many2many) to be written")
    field_label = fields.Char(compute="_compute_field_label", store=False)
    value_changed = fields.Boolean(compute="_compute_value_changed", store=False)
    change_indicator = fields.Char(compute="_compute_change_indicator", store=False)

    @api.depends("field")
    def _compute_field_label(self):
        """Convierte el nombre técnico del campo en un label legible."""
        for rec in self:
            if not rec.field:
                rec.field_label = ""
                continue
            # Mapeo de nombres técnicos a nombres legibles en español
            field_labels = {
                "name": "Nombre",
                "street": "Calle",
                "city": "Ciudad",
                "zip": "Código Postal",
                "state_id": "Provincia",
                "l10n_ar_afip_responsibility_type_id": "Responsabilidad AFIP",
                "phone": "Teléfono",
                "email": "Email",
                "vat": "CUIT",
            }
            rec.field_label = field_labels.get(rec.field, rec.field.replace("_", " ").title())

    @api.depends("old_value", "new_value")
    def _compute_value_changed(self):
        """Determina si el valor cambió."""
        for rec in self:
            rec.value_changed = rec.old_value != rec.new_value

    @api.depends("value_changed")
    def _compute_change_indicator(self):
        """Muestra un indicador visual de cambio."""
        for rec in self:
            rec.change_indicator = "→" if rec.value_changed else ""


class ResPartnerUpdateFromPadronWizard(models.TransientModel):
    _name = "res.partner.update.from.padron.wizard"
    _description = "ARCA A5 Census Wizard"

    @api.model
    def get_partners(self):
        # TODO deberiamos buscar de otro manera estos partners
        domain = [
            ("vat", "!=", False),
            ("l10n_latam_identification_type_id.l10n_ar_afip_code", "=", 80),
        ]
        active_ids = self.env.context.get("active_ids", [])
        if active_ids:
            domain.append(("id", "in", active_ids))
        return self.env["res.partner"].search(domain)

    @api.model
    def default_get(self, fields):
        res = super(ResPartnerUpdateFromPadronWizard, self).default_get(fields)
        context = self.env.context
        is_partner_context = context.get("active_model") == "res.partner" and context.get("active_ids")
        if is_partner_context:
            partners = self.get_partners()
            if not partners:
                msg = _("No se encontró ningún partner con CUIT para actualizar")
                raise UserError(msg)
            elif len(partners) == 1:
                res["state"] = "selection"
                res["partner_id"] = partners[0].id
        return res

    @api.model
    def _get_domain(self):
        fields_names = [
            "name",
            "street",
            "city",
            "zip",
            "state_id",
            "l10n_ar_afip_responsibility_type_id",
            "last_update_census",
        ]
        return [
            ("model", "=", "res.partner"),
            ("name", "in", fields_names),
        ]

    @api.model
    def _get_default_title_case(self):
        parameter = self.env["ir.config_parameter"].sudo().get_param("use_title_case_on_padron_afip")
        if parameter == "False" or parameter == "0":
            return False
        return True

    @api.model
    def get_fields(self):
        return self.env["ir.model.fields"].search(self._get_domain())

    state = fields.Selection(
        [
            ("option", "Option"),
            ("selection", "Selection"),
            ("finished", "Finished"),
        ],
        readonly=True,
        required=True,
        default="option",
    )
    field_ids = fields.One2many(
        "res.partner.update.from.padron.field",
        "wizard_id",
        string="Fields",
    )
    partner_ids = fields.Many2many(
        "res.partner",
        "partner_update_from_padron_rel",
        "update_id",
        "partner_id",
        string="Partners",
        default=get_partners,
    )
    partner_id = fields.Many2one(
        "res.partner",
        string="Partner",
        readonly=True,
    )
    update_constancia = fields.Boolean(
        default=True,
    )
    title_case = fields.Boolean(
        help="Converts retreived text fields to Title Case.",
        default=_get_default_title_case,
    )
    field_to_update_ids = fields.Many2many(
        "ir.model.fields",
        "res_partner_update_fields",
        "update_id",
        "field_id",
        string="Fields To Update",
        help="Only this fields are going to be retrived and updated",
        default=get_fields,
        domain=_get_domain,
        required=True,
    )

    @api.onchange("partner_id")
    def change_partner(self):
        self.ensure_one()
        self.field_ids.unlink()
        partner = self.partner_id
        fields_names = self.field_to_update_ids.mapped("name")
        if partner:
            partner_vals = partner.get_data_from_padron_arca()
            _logger.info(
                "=== Datos ARCA para %s ===\n" "Campos disponibles: %s\n" "Campos seleccionados: %s\n" "Valores: %s",
                partner.name,
                list(partner_vals.keys()),
                fields_names,
                partner_vals,
            )
            lines = []
            fields_names = list(set(partner_vals) & set(fields_names))
            for key in fields_names:
                old_value = partner[key]
                new_value = partner_vals[key]
                if new_value == "":
                    new_value = False
                if self.title_case and key in ("name", "city", "street"):
                    new_value = new_value and new_value.title()

                # Manejar campos Many2one para mostrar nombre en lugar de ID
                if key in ("state_id", "l10n_ar_afip_responsibility_type_id"):
                    old_value_display = old_value.display_name if old_value else ""
                    old_value_id = old_value.id if old_value else False
                    # new_value viene como ID, necesitamos buscar el nombre
                    if new_value:
                        if key == "state_id":
                            record = self.env["res.country.state"].browse(int(new_value))
                        elif key == "l10n_ar_afip_responsibility_type_id":
                            resp_model = "l10n_ar.afip.responsibility.type"
                            record = self.env[resp_model].browse(int(new_value))
                        new_value_display = record.display_name if record else str(new_value)
                        new_value_id = int(new_value)
                    else:
                        new_value_display = ""
                        new_value_id = False

                    # Comparar usando IDs
                    if old_value_id != new_value_id:
                        line_vals = {
                            "wizard_id": self.id,
                            "field": key,
                            "old_value": old_value_display,
                            "new_value": new_value_display,
                            "real_value": (str(new_value) if new_value else False),
                        }
                        lines.append((0, False, line_vals))
                elif key in ("impuestos_padron", "actividades_padron"):
                    old_value_ids = old_value.ids
                    new_value_ids = new_value if new_value else []
                    if old_value_ids != new_value_ids:
                        line_vals = {
                            "wizard_id": self.id,
                            "field": key,
                            "old_value": str(old_value_ids),
                            "new_value": str(new_value_ids),
                            "real_value": str(new_value_ids),
                        }
                        lines.append((0, False, line_vals))
                else:
                    # Campos normales (Char, Text, etc)
                    old_value_str = str(old_value) if old_value else ""
                    new_value_str = str(new_value) if new_value else ""
                    if old_value_str != new_value_str:
                        line_vals = {
                            "wizard_id": self.id,
                            "field": key,
                            "old_value": old_value_str,
                            "new_value": new_value_str,
                        }
                        lines.append((0, False, line_vals))
            self.field_ids = lines

    def _update(self):
        self.ensure_one()
        vals = {}
        for field in self.field_ids:
            if field.field in ("impuestos_padron", "actividades_padron"):
                value_to_write = field.real_value if field.real_value else field.new_value
                vals[field.field] = [(6, False, literal_eval(value_to_write))]
            elif field.field in (
                "state_id",
                "l10n_ar_afip_responsibility_type_id",
            ):
                # Para Many2one, usar real_value si existe (contiene el ID)
                value_to_write = field.real_value if field.real_value else field.new_value
                vals[field.field] = int(value_to_write) if value_to_write else False
            self.change_partner()
            self._update()

        self.write({"state": "finished"})
        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }

    def update_selection(self):
        self.ensure_one()
        if not self.field_ids:
            self.write({"state": "finished"})
            return {
                "type": "ir.actions.act_window",
                "res_model": self._name,
                "res_id": self.id,
                "view_mode": "form",
                "target": "new",
            }
        self._update()
        return self.next_cb()

    def next_cb(self):
        """ """
        self.ensure_one()
        if self.partner_id:
            self.write({"partner_ids": [(3, self.partner_id.id, False)]})
        return self._next_screen()

    def _next_screen(self):
        self.ensure_one()
        values = {}
        if self.partner_ids:
            # in this case, we try to find the next record.
            partner = self.partner_ids[0]
            values.update(
                {
                    "partner_id": partner.id,
                    "state": "selection",
                }
            )
        else:
            values.update(
                {
                    "state": "finished",
                }
            )

        self.write(values)
        # because field is not changed, view is distroyed and reopen, on change
        # is not called an we call it manually
        self.change_partner()
        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }

    def start_process_cb(self):
        """
        Start the process.
        """
        self.ensure_one()
        return self._next_screen()
