##############################################################################
# For copyright and license notices, see __manifest__.py file in module root
# directory
##############################################################################

import logging

from odoo import _, fields, models
from odoo.exceptions import UserError
from zeep.helpers import serialize_object

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

    # Constantes para servicios ARCA - Pueden ser heredadas en módulos custom
    _PADRON_SERVICE_CODE = "ws_sr_constancia_inscripcion"
    _PADRON_METHOD_NAME = "get_persona_list"

    # Separo esto para poder heredar de otros
    # modulos y extender los datos
    def parse_census_vals(self, census):
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
            "street": get_value(census, "direccion"),
            "city": get_value(census, "localidad"),
            "zip": get_value(census, "cod_postal"),
            "last_update_census": fields.Date.today(),
        }

        # Solo incluir 'name' si denominacion tiene un valor válido
        denominacion = get_value(census, "denominacion")
        if denominacion:
            vals["name"] = denominacion

        # Establecer país Argentina
        country_ar = self.env.ref("base.ar", raise_if_not_found=False)
        if country_ar:
            vals["country_id"] = country_ar.id

        # padron.idProvincia
        monotributo = get_value(census, "monotributo", "N")
        provincia = get_value(census, "provincia")
        localidad = get_value(census, "localidad")

        if provincia:
            # CABA puede tener diferentes códigos según la base de datos
            caba_codes = ["C", "CABA", "ABA"]
            # Detectar si la provincia es CABA por nombre
            provincia_upper = provincia.upper()
            caba_names = ["CAPITAL", "CIUDAD AUTONOMA", "CABA", "C.A.B.A"]
            is_caba = any(caba_name in provincia_upper for caba_name in caba_names)

            # Si no hay localidad y la provincia es CABA, establecer CABA
            if not localidad and is_caba:
                state = self.env["res.country.state"].search(
                    [
                        ("code", "in", caba_codes),
                        ("country_id.code", "=", "AR"),
                    ],
                    limit=1,
                )
                if state:
                    vals["city"] = "Ciudad Autónoma de Buenos Aires"
            # Para provincias con localidad (o sin localidad si no es CABA)
            elif localidad or not is_caba:
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
                    resp_type = self.env.ref("l10n_ar.res_RM").id
                    vals["l10n_ar_afip_responsibility_type_id"] = resp_type
                elif imp_iva == "AC":
                    resp_type = self.env.ref("l10n_ar.res_IVARI").id
                    vals["l10n_ar_afip_responsibility_type_id"] = resp_type
                elif imp_iva == "EX":
                    resp_type = self.env.ref("l10n_ar.res_IVAE").id
                    vals["l10n_ar_afip_responsibility_type_id"] = resp_type
            except Exception as e:
                msg = "No se pudo establecer tipo de responsabilidad ARCA: %s"
                _logger.warning(msg, e)

        return vals

    def _transform_arca_persona_to_census(self, persona_data):
        """Transform ARCA persona structure to census format.

        Prepares data for parse_census_vals method.

        Args:
            persona_data: Dictionary with nested ARCA response structure.

        Returns:
            Dictionary with flat structure expected by parse_census_vals.
        """
        # Validación defensiva: verificar que persona_data no sea None
        if not persona_data or not isinstance(persona_data, dict):
            msg = "ARCA no devolvió datos válidos " "(persona_data es None o inválido)"
            raise UserError(_(msg))

        # Construir denominación desde nombre y apellido
        datos_generales = persona_data.get("datosGenerales") or {}
        if not isinstance(datos_generales, dict):
            msg = "ARCA devolvió datos generales inválidos"
            raise UserError(_(msg))

        nombre = (datos_generales.get("nombre") or "").strip()
        apellido = (datos_generales.get("apellido") or "").strip()
        razon_social = (datos_generales.get("razonSocial") or "").strip()

        # Para personas jurídicas solo viene razonSocial
        # Para físicas viene nombre+apellido
        if razon_social:
            denominacion = razon_social
        elif apellido:
            denominacion = f"{apellido}, {nombre}"
        else:
            denominacion = nombre

        if not denominacion or denominacion == ", ":
            # Log para diagnóstico
            cuit = datos_generales.get("idPersona", "desconocido")
            _logger.warning(
                "ARCA no devolvió nombre válido para CUIT %s. "
                "Se omitirá actualizar el campo 'name'. datos_generales: %s",
                cuit,
                datos_generales,
            )
            # No retornar error, simplemente omitir el campo 'name'
            # Esto permite actualizar otros datos (dirección, impuestos, etc)
            denominacion = None

        # Transformar estructura anidada a formato plano
        # con validaciones defensivas
        domicilio = datos_generales.get("domicilioFiscal") or {}
        if not isinstance(domicilio, dict):
            domicilio = {}

        datos_monotributo = persona_data.get("datosMonotributo") or {}
        if not isinstance(datos_monotributo, dict):
            datos_monotributo = {}

        datos_regimen = persona_data.get("datosRegimenGeneral") or {}
        if not isinstance(datos_regimen, dict):
            datos_regimen = {}

        impuestos_list = datos_regimen.get("impuesto") or []
        if not isinstance(impuestos_list, list):
            impuestos_list = []

        # Determinar si está inscripto en IVA (impuesto 30)
        imp_iva = "S" if any(imp.get("idImpuesto") == 30 for imp in impuestos_list if isinstance(imp, dict)) else "N"

        result = {
            "direccion": domicilio.get("direccion", ""),
            "localidad": domicilio.get("localidad", ""),
            "cod_postal": domicilio.get("codPostal", ""),
            "provincia": domicilio.get("descripcionProvincia", ""),
            "monotributo": datos_monotributo.get("actividadMonotributista", "N"),
            "imp_iva": imp_iva,
            "tipoPersona": datos_generales.get("tipoPersona", ""),
        }

        # Solo incluir denominacion si tiene un valor válido
        if denominacion:
            result["denominacion"] = denominacion

        return result

    def _get_padron_service_and_method(self, service_code=None, method_name=None):
        """Obtiene el servicio y método ARCAWS para consultas.

        Args:
            service_code: Código del servicio (default: _PADRON_SERVICE_CODE)
            method_name: Nombre del método (default: _PADRON_METHOD_NAME)

        Returns:
            tuple: (arcaws, method_id)

        Raises:
            UserError: Si no se encuentra el servicio o método configurado
        """
        code = service_code or self._PADRON_SERVICE_CODE
        method = method_name or self._PADRON_METHOD_NAME

        arcaws = self.env["arcaws"].search([("code", "=", code)], limit=1)
        if not arcaws:
            msg = _("No se encontró configuración del servicio de padrón")
            raise UserError(msg)

        method_id = arcaws.method_ids.filtered(lambda m: m.name == method)
        if not method_id:
            msg = _("No se encontró el método %s configurado") % method
            raise UserError(msg)

        method_id.ensure_one()
        return arcaws, method_id

    def _validate_and_serialize_arca_response(self, res, context_info=""):
        """Valida y serializa respuesta de servicio ARCA.

        Args:
            res: Respuesta del servicio ARCA (puede ser objeto Zeep o dict)
            context_info: Información de contexto para mensajes de error

        Returns:
            list: Lista de personas desde la respuesta

        Raises:
            UserError: Si la respuesta es inválida o no contiene datos
        """
        if res is None:
            msg = _("ARCA devolvió respuesta vacía para %s")
            raise UserError(msg % context_info)

        # Serializar respuesta Zeep a diccionario Python si es necesario
        if not isinstance(res, dict):
            res = serialize_object(res)

        if not res or not isinstance(res, dict):
            msg = _("Error al serializar respuesta ARCA para %s")
            raise UserError(msg % context_info)

        # Extraer lista de personas
        personas = res.get("persona", [])
        if not personas:
            raise UserError(_("ARCA no devolvió datos para %s") % context_info)

        return personas

    def _transform_and_parse_persona_data(self, persona_data, apply_title_case=False):
        """Transforma datos de persona ARCA a valores de partner Odoo.

        Args:
            persona_data: Diccionario con datos de persona desde ARCA
            apply_title_case: Si True, aplica title case a campos de texto

        Returns:
            dict: Valores para actualizar partner

        Raises:
            UserError: Si hay error en la transformación o parseo
        """
        census_data = self._transform_arca_persona_to_census(persona_data)
        vals = self.parse_census_vals(census_data)

        # Aplicar title case si se solicita
        if apply_title_case:
            for key in ("name", "city", "street"):
                if vals.get(key):
                    vals[key] = vals[key].title()

        return vals

    def update_from_padron_arca(self):
        """Actualiza el partner desde el Padrón ARCA sin wizard."""
        self.ensure_one()
        try:
            partner_vals = self.get_data_from_padron_arca()
            self.write(partner_vals)

            # Mostrar notificación de éxito y refrescar la vista
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

    def action_update_from_padron_mass(self):
        """Actualiza múltiples partners desde Padrón ARCA.

        Permite actualizar varios contactos en una sola llamada.
        Filtra partners con CUIT válido, agrupa en lotes y consulta
        el Padrón A5 de manera masiva.
        """
        # Filtrar partners con CUIT válido (tipo 80)
        partners_with_cuit = self.filtered(
            lambda p: p.vat
            and p.l10n_latam_identification_type_id
            and p.l10n_latam_identification_type_id.l10n_ar_afip_code == "80"
        )

        if not partners_with_cuit:
            msg = "No se encontraron contactos con CUIT válido"
            raise UserError(_(msg))

        # Obtener servicio y método usando método auxiliar
        arcaws, method_id = self._get_padron_service_and_method()

        # Procesar en lotes de 100 CUITs (límite de ARCA)
        batch_size = 100
        updated_count = 0
        error_details = []

        # Dividir en lotes
        for i in range(0, len(partners_with_cuit), batch_size):
            batch_partners = partners_with_cuit[i : i + batch_size]
            cuit_list = [p.ensure_vat() for p in batch_partners]

            try:
                # Llamar al servicio ARCA con la lista de CUITs
                res = method_id.call_arca_method(
                    obj=batch_partners[0],
                    extra_values={"cuit_list": cuit_list},
                )

                # Validar y serializar respuesta usando método auxiliar
                try:
                    lote_num = i // batch_size + 1
                    personas = self._validate_and_serialize_arca_response(res, f"lote {lote_num}")
                except UserError as ue:
                    error_details.append(str(ue))
                    _logger.error("Error validando respuesta ARCA: %s", ue)
                    continue

                # Crear diccionario CUIT -> datos
                # (filtrar elementos None o inválidos)
                persona_by_cuit = {}
                for p in personas:
                    if not p or not isinstance(p, dict):
                        continue
                    datos_generales = p.get("datosGenerales")
                    if not datos_generales or not isinstance(datos_generales, dict):
                        _logger.warning(
                            "ARCA devolvió persona sin datosGenerales válidos en lote %s: %s",
                            lote_num,
                            p,
                        )
                        continue
                    id_persona = datos_generales.get("idPersona")
                    if id_persona:
                        persona_by_cuit[str(id_persona)] = p

                # Actualizar cada partner del lote
                for partner in batch_partners:
                    try:
                        partner_cuit = partner.ensure_vat()
                        persona_data = persona_by_cuit.get(partner_cuit)

                        if not persona_data:
                            msg = "CUIT %s: Sin datos en respuesta ARCA"
                            error_details.append(_(msg) % partner_cuit)
                            continue

                        # Transformar y parsear usando método auxiliar
                        vals = partner._transform_and_parse_persona_data(persona_data)
                        # Actualizar sin tracking para evitar diálogos confusos
                        partner.with_context(tracking_disable=True).write(vals)
                        updated_count += 1

                    except Exception as e:
                        msg = "CUIT %s: %s"
                        error_details.append(_(msg) % (partner_cuit, str(e)))
                        _logger.warning("Error actualizando partner %s: %s", partner.id, e)

            except Exception as e:
                # Error en todo el lote
                error_details.append(_("Error procesando lote: %s") % str(e))
                _logger.error("Error en lote de actualización masiva: %s", e)

        # Preparar mensaje de resultado
        error_count = len(error_details)

        if updated_count > 0 and error_count == 0:
            title = _("✓ Actualización exitosa")
            message = _("Se actualizaron %d contactos desde el Padrón ARCA") % updated_count
            msg_type = "success"
            sticky = False
        elif updated_count > 0 and error_count > 0:
            title = _("⚠ Actualización parcial")
            message = _("Se actualizaron %d contactos correctamente.\n" "Se encontraron %d errores:\n\n%s") % (
                updated_count,
                error_count,
                "\n".join(error_details[:10]),
            )
            if len(error_details) > 10:
                message += _("\n... y %d errores más") % (len(error_details) - 10)
            msg_type = "warning"
            sticky = True
        else:
            title = _("✗ Error en la actualización")
            message = _("No se pudo actualizar ningún contacto.\n" "Errores encontrados:\n\n%s") % "\n".join(
                error_details[:10]
            )
            if len(error_details) > 10:
                message += _("\n... y %d errores más") % (len(error_details) - 10)
            msg_type = "danger"
            sticky = True

        # Recargar vista y mostrar notificación
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": title,
                "message": message,
                "type": msg_type,
                "sticky": sticky,
                "next": {
                    "type": "ir.actions.act_window",
                    "res_model": "res.partner",
                    "name": _("Contactos"),
                    "view_mode": "list,form",
                    "views": [[False, "list"], [False, "form"]],
                    "target": "current",
                    "domain": [],
                    "context": {},
                },
            },
        }

    def get_data_from_padron_arca(self):
        """Get partner data from ARCA Padrón A5.

        Uses get_persona_list method with single CUIT for consistency.

        Returns:
            dict: Partner values to update

        Raises:
            UserError: If data cannot be retrieved or parsed
        """
        self.ensure_one()
        cuit = self.ensure_vat()

        # Obtener servicio y método usando método auxiliar
        arcaws, method_id = self._get_padron_service_and_method()

        error_msg = _(
            "No pudimos actualizar desde padrón ARCA al partner %s (%s).\n"
            "Recomendamos verificar manualmente en la página de ARCA.\n"
            "Obtuvimos este error: %s"
        )

        try:
            # Llamar con lista de un solo CUIT
            res = method_id.call_arca_method(obj=self, extra_values={"cuit_list": [cuit]})

            # Validar y serializar respuesta usando método auxiliar
            personas = self._validate_and_serialize_arca_response(res, cuit)
            persona_data = personas[0] if personas else None

            if not persona_data or not isinstance(persona_data, dict):
                msg = _("ARCA no devolvió datos válidos para el CUIT %s")
                raise UserError(msg % cuit)

            # Log para diagnóstico
            _logger.debug(
                "=== Respuesta ARCA para CUIT %s ===\n" "Datos generales: %s\n" "=== Fin respuesta ARCA ===",
                cuit,
                persona_data.get("datosGenerales", {}),
            )

            # Transformar y parsear usando método auxiliar
            # (sin modificar el casing)
            return self._transform_and_parse_persona_data(persona_data, apply_title_case=False)

        except UserError:
            # Re-raise UserError sin modificar
            raise
        except Exception as e:
            _logger.warning(
                "Error obteniendo datos ARCA para CUIT %s: %s",
                cuit,
                e,
            )
            raise UserError(error_msg % (self.name, cuit, str(e)))

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
