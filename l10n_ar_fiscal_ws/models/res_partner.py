##############################################################################
# For copyright and license notices, see __manifest__.py file in module root
# directory
##############################################################################

import logging

from odoo import _, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class ResPartner(models.Model):
    _inherit = "res.partner"

    mipyme_required = fields.Boolean(
        string="Must credit invoice",
    )
    mipyme_from_amount = fields.Float(
        string="Credit invoice from amount",
    )
    last_update_census = fields.Date(string="Last update census")

    # Separo esto para poder heredar de otros
    # modulos y extender los datos
    def parce_census_vals(self, census):
        """Parse census data from ARCA Padrón A5.

        Args:
            census: Dictionary with ARCA response data. Expected keys:
                - denominacion: Company/person name
                - direccion: Fiscal address
                - localidad: City
                - cod_postal: ZIP code
                - provincia: Province name
                - imp_iva: VAT status (S=Active, N=Not inscribed)
                - impuestos: List of tax IDs [10, 11, 12, etc]
                - monotributo: Monotributo status (S/N)
        """

        # Soportar tanto diccionarios como objetos con atributos
        def get_value(data, key, default=""):
            if isinstance(data, dict):
                return data.get(key, default)
            return getattr(data, key, default)

        # porque imp_iva activo puede ser S o AC
        imp_iva = get_value(census, "imp_iva", "N")
        if imp_iva == "S":
            imp_iva = "AC"
        elif imp_iva == "N":
            # por ej. monotributista devuelve N
            imp_iva = "NI"

        vals = {
            "name": get_value(census, "denominacion"),
            "street": get_value(census, "direccion"),
            "city": get_value(census, "localidad"),
            "zip": get_value(census, "cod_postal"),
            "last_update_census": fields.Date.today(),
        }

        # Establecer país Argentina
        country_ar = self.env.ref("base.ar", raise_if_not_found=False)
        if country_ar:
            vals["country_id"] = country_ar.id

        # padron.idProvincia
        monotributo = get_value(census, "monotributo", "N")
        provincia = get_value(census, "provincia")
        localidad = get_value(census, "localidad")

        if provincia:
            # depending on the database, caba can have one of this codes
            caba_codes = ["C", "CABA", "ABA"]
            # if not localidad then it should be CABA.
            if not localidad:
                state = self.env["res.country.state"].search(
                    [
                        ("code", "in", caba_codes),
                        ("country_id.code", "=", "AR"),
                    ],
                    limit=1,
                )
                # Para CABA sin localidad, establecer CABA
                if state:
                    vals["city"] = "Ciudad Autónoma de Buenos Aires"
            # If localidad cant be caba
            else:
                state = self.env["res.country.state"].search(
                    [
                        ("name", "ilike", provincia),
                        ("code", "not in", caba_codes),
                        ("country_id.code", "=", "AR"),
                    ],
                    limit=1,
                )
            if state:
                vals["state_id"] = state.id

        # Intentar determinar tipo de responsabilidad ARCA basado
        # en IVA y monotributo. Solo si el campo existe en el modelo
        # (puede estar en l10n_ar u otro módulo)
        partner_fields = self.env["res.partner"]._fields
        if partner_fields.get("l10n_ar_afip_responsibility_type_id"):
            try:
                if imp_iva == "NI" and monotributo == "S":
                    vals["l10n_ar_afip_responsibility_type_id"] = self.env.ref("l10n_ar.res_RM").id
                elif imp_iva == "AC":
                    vals["l10n_ar_afip_responsibility_type_id"] = self.env.ref("l10n_ar.res_IVARI").id
                elif imp_iva == "EX":
                    vals["l10n_ar_afip_responsibility_type_id"] = self.env.ref("l10n_ar.res_IVAE").id
            except Exception as e:
                msg = "Could not set ARCA responsibility type: %s"
                _logger.warning(msg, e)

        return vals

    def update_from_padron_arca(self):
        """Actualiza el partner desde el Padrón ARCA sin wizard."""
        self.ensure_one()
        try:
            partner_vals = self.get_data_from_padron_arca()

            # Aplicar title case si está configurado
            param = "use_title_case_on_padron_afip"
            parameter = self.env["ir.config_parameter"].sudo().get_param(param)
            title_case = parameter and parameter != "False" and parameter != "0"

            if title_case:
                for key in ("name", "city", "street"):
                    if key in partner_vals and partner_vals[key]:
                        partner_vals[key] = partner_vals[key].title()

            # Actualizar el partner
            self.write(partner_vals)

            # Refrescar el formulario para mostrar los cambios
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("Actualización exitosa"),
                    "message": _("Datos actualizados desde el Padrón ARCA"),
                    "type": "success",
                    "sticky": False,
                    "next": {
                        "type": "ir.actions.act_window",
                        "res_model": "res.partner",
                        "res_id": self.id,
                        "view_mode": "form",
                        "views": [[False, "form"]],
                        "target": "current",
                    },
                },
            }
        except UserError:
            raise
        except Exception as e:
            error_msg = _("Error al actualizar desde el Padrón ARCA:\n%s")
            raise UserError(error_msg % str(e))

    def get_data_from_padron_arca(self):
        self.ensure_one()
        cuit = self.ensure_vat()

        # consultamos a5 ya que extiende a4 y tiene validez de constancia
        code = "ws_sr_constancia_inscripcion"
        arcaws = self.env["arcaws"].search([("code", "=", code)])
        if not arcaws:
            msg = _("No se encontró configuración del servicio de padrón")
            raise UserError(msg)

        method_id = arcaws.method_ids.filtered(lambda m: m.name == "get_persona")
        if not method_id:
            msg = _("No se encontró el método get_persona configurado")
            raise UserError(msg)

        error_msg = _(
            "No pudimos actualizar desde padrón ARCA al partner %s (%s).\n"
            "Recomendamos verificar manualmente en la página de ARCA.\n"
            "Obtuvimos este error: %s"
        )

        try:
            res = method_id.call_arca_method(obj=self, extra_values={"cuit": cuit})
        except Exception as e:
            raise UserError(error_msg % (self.name, cuit, e))

        # Log completo para diagnosticar qué datos devuelve ARCA
        _logger.info(
            "=== Respuesta ARCA completa para CUIT %s ===\n"
            "Claves: %s\n"
            "nombre: %s\n"
            "apellido: %s\n"
            "tipoPersona: %s\n"
            "direccion: %s\n"
            "localidad: %s\n"
            "cod_postal: %s\n"
            "provincia: %s\n"
            "imp_iva: %s\n"
            "impuestos: %s\n"
            "monotributo: %s\n"
            "actividades: %s\n"
            "=== Fin respuesta ARCA ===",
            cuit,
            list(res.keys()) if isinstance(res, dict) else type(res),
            res.get("nombre"),
            res.get("apellido"),
            res.get("tipoPersona"),
            res.get("direccion"),
            res.get("localidad"),
            res.get("cod_postal"),
            res.get("provincia"),
            res.get("imp_iva"),
            res.get("impuestos"),
            res.get("monotributo"),
            res.get("actividades"),
        )

        # Construir denominación desde nombre y apellido
        nombre = (res.get("nombre") or "").strip()
        apellido = (res.get("apellido") or "").strip()

        # Para personas jurídicas solo viene nombre, para físicas
        # nombre+apellido
        if apellido:
            denominacion = f"{apellido}, {nombre}"
        else:
            denominacion = nombre

        if not denominacion or denominacion == ", ":
            _logger.warning(
                "ARCA no devolvió nombre válido para CUIT %s. Respuesta: %s",
                cuit,
                res,
            )
            error_detail = "La ARCA no devolvió nombre válido"
            raise UserError(error_msg % (self.name, cuit, error_detail))

        # Agregar denominación al resultado para parce_census_vals
        res["denominacion"] = denominacion
        vals = self.parce_census_vals(res)
        return vals

    def l10n_ar_fiscal_ws_fe_min_ammount(self):
        for record in self:
            if record.l10n_ar_vat:
                ws = self.env.company.arca_get_connection("wsfecred")
                res = ws.call_arca_service(
                    "ConsultarMontoObligadoRecepcion",
                    {
                        "cuitConsultada": record.l10n_ar_vat,
                        "fechaEmision": fields.Date.today(),
                    },
                )
                return res
                # record.mipyme_required = True if
                # ws.Resultado == "S" else False
                # record.mipyme_from_amount = float(res)
