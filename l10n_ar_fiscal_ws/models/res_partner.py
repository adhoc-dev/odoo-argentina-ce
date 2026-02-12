##############################################################################
# For copyright and license notices, see __manifest__.py file in module root
# directory
##############################################################################

import logging
import re

from odoo import _, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class ResPartner(models.Model):
    _inherit = "res.partner"

    # Constantes para el servicio de padrón ARCA
    _PADRON_SERVICE_CODE = "ws_sr_constancia_inscripcion"
    _PADRON_METHOD_NAME = "get_persona_list"
    _PADRON_BATCH_SIZE = 100  # Límite de ARCA para consultas masivas
    _PADRON_MAX_ERRORS_TO_SHOW = 10  # Mostrar primeros N errores en UI

    # Constantes para el servicio de FCE (Factura de Crédito Electrónica)
    _FECRED_SERVICE_CODE = "wsfecred"
    _FECRED_METHOD_NAME = "consultar_monto_obligado_recepcion"

    mipyme_required = fields.Boolean(
        string="Must credit invoice",
    )
    mipyme_from_amount = fields.Float(
        string="Credit invoice from amount",
    )
    last_update_census = fields.Date(string="Last update census")

    # Separo esto para poder heredar de otros
    # modulos y extender los datos
    def parse_census_vals(self, census):
        """Parse census data from ARCA Padrón A5.

        Expected format:
                - direccion: Street address
                - localidad: City name
                - cod_postal: Postal code
                - provincia: Province name
                - imp_iva: VAT status (S=Active, N=Not inscribed)
                - impuestos: List of tax IDs [10, 11, 12, etc]
                - monotributo: Monotributo status (S/N)

        Args:
            census: Dict with flat data structure

        Returns:
            dict: Processed values ready for partner update
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
            "imp_iva_padron": imp_iva,
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

            state = False
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
            except (ValueError, UserError, KeyError, AttributeError) as e:
                msg = "No se pudo establecer tipo de responsabilidad ARCA: %s"
                _logger.warning(msg, e)
            except Exception:
                _logger.exception("Unexpected error al establecer tipo de responsabilidad ARCA")
                raise

        return vals

    def _build_denominacion(self, datos_generales):
        """Construye la denominación desde datos ARCA.

        Args:
            datos_generales: Dict con nombre, apellido, razonSocial

        Returns:
            str o None: Denominación construida o None si no hay datos válidos
        """
        if not isinstance(datos_generales, dict):
            return None

        nombre = (datos_generales.get("nombre") or "").strip()
        apellido = (datos_generales.get("apellido") or "").strip()
        razon_social = (datos_generales.get("razonSocial") or "").strip()

        # Personas jurídicas: solo razonSocial
        if razon_social:
            return razon_social

        # Personas físicas: apellido, nombre
        if apellido:
            return f"{apellido}, {nombre}" if nombre else apellido

        # Solo nombre sin apellido
        if nombre:
            return nombre

        # Sin datos válidos
        return None

    def _transform_arca_persona_to_census_safe(self, persona_data):
        """Versión segura que transforma datos ARCA a formato census.

        Acepta objetos zeep o dicts. Nunca lanza excepciones.

        Returns:
            dict con estructura plana, o dict con 'afip_error'.
        """
        try:
            # Serializar objeto zeep a dict si es necesario
            if persona_data and not isinstance(persona_data, dict):
                try:
                    from zeep.helpers import serialize_object

                    persona_data = serialize_object(
                        persona_data,
                        target_cls=dict,
                    )
                except Exception as e:
                    _logger.error(
                        "Error serializando persona_data: %s",
                        e,
                    )
                    return {"afip_error": (f"Error al serializar datos ARCA: {e}")}

            # Validación defensiva
            if not persona_data or not isinstance(persona_data, dict):
                return {"afip_error": "ARCA no devolvió datos válidos"}

            # Construir denominación desde nombre y apellido
            datos_generales = persona_data.get("datosGenerales") or {}
            if not isinstance(datos_generales, dict):
                return {"afip_error": "ARCA devolvió datos generales inválidos"}

            # Usar método auxiliar para construir denominación
            denominacion = self._build_denominacion(datos_generales)

            if not denominacion:
                # Log para diagnóstico
                cuit = datos_generales.get("idPersona", "desconocido")
                _logger.warning(
                    "ARCA no devolvió nombre válido para CUIT %s. "
                    "Se omitirá actualizar el campo 'name'. datos_generales: %s",
                    cuit,
                    datos_generales,
                )

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
            iva_activo = any(imp.get("idImpuesto") == 30 for imp in impuestos_list if isinstance(imp, dict))
            imp_iva = "S" if iva_activo else "N"

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

        except Exception as e:
            _logger.error("Error en transformación segura de datos ARCA: %s", e, exc_info=True)
            return {"afip_error": f"Error al transformar datos de ARCA: {str(e)}"}

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
            msg = _("No se encontró configuración para el servicio '%s'") % code
            raise UserError(msg)

        method_id = arcaws.method_ids.filtered(lambda m: m.name == method)
        if not method_id:
            msg = _("No se encontró el método %s configurado") % method
            raise UserError(msg)

        method_id.ensure_one()
        return arcaws, method_id

    def _validate_and_serialize_arca_response(self, res, context_info="", single=False):
        """Valida y serializa respuesta de servicio ARCA.

        Args:
            res: Respuesta del servicio ARCA (puede ser objeto Zeep o dict)
            context_info: Información de contexto para mensajes de error
            single: Si True, retorna directamente el primer elemento

        Returns:
            list o dict: Lista de personas o persona única si single=True

        Raises:
            UserError: Si la respuesta es inválida o no contiene datos
        """
        if res is None:
            msg = _("ARCA devolvió respuesta vacía para %s")
            raise UserError(msg % context_info)

        # Serializar respuesta Zeep a diccionario Python si es necesario
        if not isinstance(res, dict):
            from zeep.helpers import serialize_object

            res = serialize_object(res, target_cls=dict)

        if not res or not isinstance(res, dict):
            msg = _("Error al serializar respuesta ARCA para %s")
            raise UserError(msg % context_info)

        # Extraer lista de personas
        personas = res.get("persona", [])
        if not personas:
            raise UserError(_("ARCA no devolvió datos para %s") % context_info)

        # Retornar primer elemento si se espera uno solo
        if single:
            if len(personas) > 1:
                _logger.warning(
                    "ARCA devolvió %d personas para %s, esperando 1. Se usará la primera.",
                    len(personas),
                    context_info,
                )
            first_persona = personas[0]
            # Validación adicional para homologación
            if first_persona is None:
                _logger.error(
                    "ARCA devolvió persona None en posición 0 para %s. Respuesta completa: %s",
                    context_info,
                    personas,
                )
            return first_persona

        return personas

    def _transform_and_parse_persona_data(self, persona_data):
        """Transforma datos de persona ARCA a valores de partner Odoo sin alterar el casing.

        Args:
            persona_data: Diccionario con datos de persona desde ARCA

        Returns:
            dict: Valores para actualizar partner

        Raises:
            UserError: Si hay error en la transformación o parseo
        """
        census_data = self._transform_arca_persona_to_census(persona_data)
        vals = self.parse_census_vals(census_data)
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

        # Usar método auxiliar para construir denominación
        denominacion = self._build_denominacion(datos_generales)

        if not denominacion:
            # Log para diagnóstico
            cuit = datos_generales.get("idPersona", "desconocido")
            _logger.warning(
                "ARCA no devolvió nombre válido para CUIT %s. "
                "Se omitirá actualizar el campo 'name'. datos_generales: %s",
                cuit,
                datos_generales,
            )

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
        iva_activo = any(imp.get("idImpuesto") == 30 for imp in impuestos_list if isinstance(imp, dict))
        imp_iva = "S" if iva_activo else "N"

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

    def get_data_from_padron_arca_safe(self):
        """Versión segura de get_data_from_padron_arca.

        En caso de error de AFIP/ARCA, devuelve un dict con la clave
        'afip_error' en lugar de lanzar una UserError.
        """
        self.ensure_one()
        try:
            cuit = self.ensure_vat()
        except UserError as e:
            _logger.error("CUIT inválido o faltante para partner %s: %s", self.name, e)
            return {
                "afip_error": str(e),
                "xml_request": "",
                "xml_response": "",
            }
        company = self.env.company

        # 1) Obtener servicio y método
        try:
            arcaws, method = self._get_padron_service_and_method()
        except UserError as e:
            _logger.error("Error al obtener servicio ARCA: %s", e)
            return {
                "afip_error": str(e),
                "xml_request": "",
                "xml_response": "",
            }

        # 2) Llamar a ARCA
        try:
            result = method.call_arca_method(
                self,
                company_id=company,
                extra_values={"cuit_list": [cuit]},
            )
        except UserError as e:
            _logger.error(
                "UserError en llamada ARCA para CUIT %s: %s",
                cuit,
                e,
            )
            return {
                "afip_error": str(e),
                "xml_request": "",
                "xml_response": "",
            }
        except Exception as e:
            _logger.error(
                "Excepción en llamada ARCA para CUIT %s: %s",
                cuit,
                e,
            )
            return {
                "afip_error": f"Error inesperado: {e}",
                "xml_request": "",
                "xml_response": "",
            }

        # 3) Extraer XMLs (siempre)
        xml_request = getattr(result, "xml_request", "") or ""
        xml_response = getattr(result, "xml_response", "") or ""

        # 4) Verificar errores en el XML de respuesta
        if xml_response and ("<error>" in xml_response or "<errorConstancia>" in xml_response):
            _logger.warning(
                "ERROR DETECTADO EN XML RESPONSE para CUIT %s",
                cuit,
            )
            error_match = re.search(
                r"<error[^>]*>([^<]+)</error>",
                xml_response,
            )
            if error_match:
                error_msg = error_match.group(1).strip()
                _logger.warning(
                    "ERROR AFIP para CUIT %s: %s",
                    cuit,
                    error_msg,
                )
                return {
                    "afip_error": error_msg,
                    "xml_request": xml_request,
                    "xml_response": xml_response,
                }
            return {
                "afip_error": "Error devuelto por AFIP (ver XML)",
                "xml_request": xml_request,
                "xml_response": xml_response,
            }

        # 5) Extraer datos de persona
        if not result or not hasattr(result, "persona") or not result.persona:
            _logger.warning(
                "NO SE ENCONTRARON DATOS DE PERSONA para CUIT %s. " "Atributos de result: %s",
                cuit,
                dir(result) if result else "None",
            )
            return {
                "afip_error": (f"No se encontraron datos para CUIT {cuit}"),
                "xml_request": xml_request,
                "xml_response": xml_response,
            }

        persona_data = result.persona[0] if isinstance(result.persona, list) else result.persona

        # 6) Transformar y parsear los datos
        try:
            census_data = self._transform_arca_persona_to_census_safe(
                persona_data,
            )
            if census_data.get("afip_error"):
                return {
                    "afip_error": census_data["afip_error"],
                    "xml_request": xml_request,
                    "xml_response": xml_response,
                }

            vals = self.parse_census_vals(census_data)
            vals["xml_request"] = xml_request
            vals["xml_response"] = xml_response

            return vals

        except Exception as parse_error:
            _logger.error(
                "ERROR PARSEANDO para CUIT %s: %s",
                cuit,
                parse_error,
                exc_info=True,
            )
            return {
                "afip_error": f"Error procesando datos: {parse_error}",
                "xml_request": xml_request,
                "xml_response": xml_response,
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

            # Validar y serializar respuesta (single=True retorna directamente)
            persona_data = self._validate_and_serialize_arca_response(res, cuit, single=True)

            # Validación adicional: ARCA en homologación puede devolver estructura vacía
            if not persona_data or not isinstance(persona_data, dict):
                _logger.error(
                    "ARCA devolvió persona_data inválido para CUIT %s: %s (tipo: %s)",
                    cuit,
                    persona_data,
                    type(persona_data),
                )
                raise UserError(
                    _(
                        "ARCA no devolvió datos válidos para el CUIT %s. "
                        "Esto puede ocurrir en ambiente de homologación con CUITs de prueba."
                    )
                    % cuit
                )

            # Log estructurado solo en modo debug
            if _logger.isEnabledFor(logging.DEBUG):
                dg = persona_data.get("datosGenerales") or {}
                denominacion = self._build_denominacion(dg) if isinstance(dg, dict) else None
                _logger.debug(
                    "ARCA Padrón A5 - CUIT: %s | Tipo: %s | Nombre: %s",
                    cuit,
                    dg.get("tipoPersona") if isinstance(dg, dict) else "?",
                    denominacion or "(sin nombre)",
                )

            # Transformar y parsear usando método auxiliar
            # (sin modificar el casing)
            return self._transform_and_parse_persona_data(persona_data)

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

    def update_multiple_from_padron_arca(self):
        """Actualiza múltiples partners desde AFIP en lotes.

        Usando getPersonaList_v2, procesando en lotes de _PADRON_BATCH_SIZE.

        Returns:
            dict: Notificación con resultado de la operación
        """
        if not self:
            return

        company = self.env.company
        cuit_list = []
        partner_by_cuit = {}

        # Recolectar CUITs válidos
        for partner in self:
            try:
                cuit = partner.ensure_vat()
                cuit_list.append(cuit)
                partner_by_cuit[cuit] = partner
            except Exception as e:
                _logger.warning(
                    "Partner %s (ID: %s) no tiene CUIT válido: %s",
                    partner.name,
                    partner.id,
                    e,
                )
                continue

        if not cuit_list:
            raise UserError(_("No hay partners con CUIT válido para actualizar"))

        # Obtener el método get_persona_list
        method = self.env["arcaws.method"].search(
            [("arcaws_id.code", "=", "ws_sr_constancia_inscripcion"), ("name", "=", "get_persona_list")], limit=1
        )

        if not method:
            raise UserError(
                _("El método 'get_persona_list' no está configurado para el servicio de Constancia de Inscripción")
            )

        # Procesar en lotes para respetar el límite de ARCA (_PADRON_BATCH_SIZE)
        updated = 0
        errors = []

        for start in range(0, len(cuit_list), self._PADRON_BATCH_SIZE):
            batch_cuits = cuit_list[start : start + self._PADRON_BATCH_SIZE]
            try:
                result = method.call_arca_method(self[0], company_id=company, extra_values={"cuit_list": batch_cuits})
            except Exception as batch_exc:
                error_msg = _("Error al consultar ARCA para lote %d-%d: %s") % (
                    start + 1,
                    min(start + self._PADRON_BATCH_SIZE, len(cuit_list)),
                    str(batch_exc),
                )
                errors.append(error_msg)
                _logger.error(error_msg)
                continue

            if result and hasattr(result, "persona"):
                personas = result.persona if isinstance(result.persona, list) else [result.persona]

                for persona_data in personas:
                    cuit = str(persona_data.idPersona)
                    partner = partner_by_cuit.get(cuit)
                    if partner:
                        try:
                            vals = partner._transform_and_parse_persona_data(persona_data)
                            partner.write(vals)
                            updated += 1
                        except Exception as e:
                            error_msg = f"Error actualizando partner {partner.name} (CUIT: {cuit}): {str(e)}"
                            errors.append(error_msg)
                            _logger.error("Error actualizando partner %s (CUIT: %s): %s", partner.name, cuit, e)

        # Notificar resultado
        message = _("Se actualizaron %d de %d partners correctamente") % (updated, len(cuit_list))
        notification_type = "success" if updated > 0 else "warning"

        if errors:
            message += "\n\n" + _("Errores encontrados:") + "\n- " + "\n- ".join(errors[:5])
            if len(errors) > 5:
                message += f"\n... y {len(errors) - 5} errores más."
            notification_type = "warning"

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "message": message,
                "type": notification_type,
                "sticky": False,
            },
        }

    def check_mipyme_status(self):
        """Consulta en ARCA si el partner requiere FCE y el monto mínimo."""
        partners = self.filtered("l10n_ar_vat")
        if not partners:
            return

        _arcaws, method = self._get_padron_service_and_method(
            service_code=self._FECRED_SERVICE_CODE,
            method_name=self._FECRED_METHOD_NAME,
        )
        errors = []
        for record in partners:
            try:
                res = method.call_arca_method(record)

                # Verificar errores en la respuesta ARCA
                arca_errors = getattr(res, "arrayErrores", None)
                if arca_errors and getattr(arca_errors, "codigoDescripcion", None):
                    error_descs = [getattr(e, "descripcion", str(e)) for e in arca_errors.codigoDescripcion]
                    errors.append(f"{record.l10n_ar_vat}: {', '.join(error_descs)}")
                    continue

                obligado = getattr(res, "obligado", None)
                monto_desde = getattr(res, "montoDesde", None)

                record.mipyme_required = obligado == "S"
                record.mipyme_from_amount = float(monto_desde) if monto_desde is not None else 0.0

            except Exception as e:
                errors.append(f"{record.l10n_ar_vat}: {e}")
                _logger.error("Error consultando MiPyme para %s: %s", record.l10n_ar_vat, e)

        if errors:
            raise UserError(
                _("Errores al consultar estado MiPyme:\n%s") % "\n".join(errors[: self._PADRON_MAX_ERRORS_TO_SHOW])
            )
