# Implementación Padrón A5 (Constancia de Inscripción) usando arquitectura ARCAWS

## Descripción General

Este documento describe cómo implementar la consulta al Padrón AFIP Alcance 5 (Constancia de Inscripción) utilizando la arquitectura estándar de `arcaws` en Odoo 19, siguiendo el mismo patrón que los servicios de facturación electrónica.

## Arquitectura ARCAWS

La arquitectura ARCAWS centraliza la configuración de servicios web de AFIP en la base de datos:

### Componentes principales:

1. **`arcaws`**: Modelo que define el servicio web (URLs, código, parámetros)
2. **`arcaws.method`**: Métodos disponibles del servicio (dummy, consultas, etc.)
3. **`arcaws.connection`**: Manejo automático de conexiones, tokens y autenticación
4. **`call_arca_method()`**: Método estándar para invocar servicios AFIP

### Flujo de ejecución:

```
Partner/Journal → arcaws.search([code]) → method_ids.filtered(name)
    → call_arca_method(obj, extra_values) → connection.call_arca_service()
```

## Implementación para Padrón A5

### 1. Definición del servicio en datos XML

**Archivo:** `l10n_ar_fiscal_ws/data/arcaws_padron_a5.xml`

```xml
<?xml version="1.0" encoding="utf-8"?>
<odoo>

    <!-- Servicio de Consulta de Padrón A5 / Constancia de Inscripción -->
    <record id="ws_sr_padron_a5" model="arcaws">
        <field name="name">Servicio de Consulta de Padrón Alcance 5 (Constancia de Inscripción)</field>
        <field name="code">ws_sr_padron_a5</field>
        <field name="production_url">https://aws.afip.gov.ar/sr-padron/webservices/personaServiceA5</field>
        <field name="homologation_url">https://awshomo.afip.gov.ar/sr-padron/webservices/personaServiceA5</field>
        <field name="active" eval="True"/>
    </record>

    <!-- Método Dummy para verificación -->
    <record id="ws_sr_padron_a5_dummy" model="arcaws.method">
        <field name="name">dummy</field>
        <field name="arcaws_id" ref="ws_sr_padron_a5"/>
        <field name="method_name">dummy</field>
    </record>

    <!-- Método: Obtener datos de persona por CUIT -->
    <record id="ws_sr_padron_a5_get_persona" model="arcaws.method">
        <field name="name">get_persona</field>
        <field name="arcaws_id" ref="ws_sr_padron_a5"/>
        <field name="method_name">getPersona_v2</field>
    </record>

</odoo>
```

**Notas importantes:**
- No es necesario definir `definition_dict` ni `response_dict` en XML si se manejan dinámicamente
- El framework `arcaws` maneja automáticamente Auth (Token, Sign, Cuit)
- Los `extra_values` se pasan desde el código Python

### 2. Implementación en el modelo Python

**Archivo:** `l10n_ar_fiscal_ws/models/res_partner.py`

```python
from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.addons.l10n_ar_fiscal_ws.models.exceptions import ArcaError

class ResPartner(models.Model):
    _inherit = "res.partner"

    def get_data_from_padron_arca(self):
        """Obtiene datos del padrón ARCA usando la arquitectura de arcaws"""
        self.ensure_one()
        cuit = self.ensure_vat()
        company = self.env.company

        # 1. Obtener el servicio web configurado
        arcaws = self.env['arcaws'].search([('code', '=', 'ws_sr_padron_a5')], limit=1)
        if not arcaws:
            raise UserError(_("No se encontró la configuración del Web Service de Padrón A5"))

        # 2. Obtener el método específico
        method_id = arcaws.method_ids.filtered(lambda m: m.name == 'get_persona')
        if not method_id:
            raise UserError(_("No se encontró el método 'get_persona' para el WS de Padrón A5"))

        try:
            # 3. Llamar al método ARCA
            # La conexión, token y sign se manejan automáticamente
            response = method_id.call_arca_method(
                obj=self,
                extra_values={'cuit': cuit}
            )

            # 4. Validar respuesta
            if not response or not response.get('persona'):
                raise UserError(
                    _("No se pudieron obtener datos desde el padrón ARCA para %s (CUIT: %s)")
                    % (self.name, cuit)
                )

            # 5. Procesar datos recibidos
            persona = response.get('persona')
            vals = self.parce_census_vals(persona)

            return vals

        except ArcaError as e:
            # Manejo específico de errores ARCA
            raise UserError(_("Error ARCA: %s") % str(e))
        except Exception as e:
            # Manejo de otros errores
            raise UserError(_("Error al consultar padrón: %s") % str(e))
```

### 3. Actualizar manifest

**Archivo:** `l10n_ar_fiscal_ws/__manifest__.py`

```python
{
    # ...existing code...
    "data": [
        # ...existing files...
        "data/arcaws.xml",
        "data/arcaws_padron_a5.xml",  # ← Agregar esta línea
    ],
    "version": "1.0.1",  # ← Incrementar versión
}
```

## Patrón de implementación (basado en account_journal.py)

### Ejemplo de referencia - Facturación Electrónica:

```python
def _get_last_invoice_number(self, l10n_latam_document_type):
    self.ensure_one()
    if not self.arcaws:
        raise ArcaError(_("No ARCA WS selected"))

    # 1. Buscar el método
    method_id = self.arcaws.method_ids.filtered(lambda m: m.name == "last_invoice")

    if method_id:
        # 2. Llamar con extra_values
        response = method_id.call_arca_method(
            obj=self,
            extra_values={"l10n_latam_document_type": l10n_latam_document_type.code}
        )
        return response
    else:
        raise ArcaError(_("No 'last_invoice' method defined"))
```

### Aplicado a Padrón A5:

```python
def get_data_from_padron_arca(self):
    # 1. Buscar el arcaws
    arcaws = self.env['arcaws'].search([('code', '=', 'ws_sr_padron_a5')], limit=1)

    # 2. Filtrar el método
    method_id = arcaws.method_ids.filtered(lambda m: m.name == 'get_persona')

    # 3. Llamar con extra_values
    response = method_id.call_arca_method(
        obj=self,
        extra_values={'cuit': cuit}
    )
```

## Ventajas de esta arquitectura

✅ **Configuración centralizada**: Todo en la base de datos
✅ **Reutilización**: Mismo patrón para todos los WS AFIP
✅ **Mantenibilidad**: Cambios en XML, no en código Python
✅ **Automatización**: Token, Sign, Auth manejados internamente
✅ **Consistencia**: Mismo patrón que facturación electrónica
✅ **Escalabilidad**: Fácil agregar nuevos métodos

## Contexto de ejecución en call_arca_method()

El método `call_arca_method()` en `arcaws.py` proporciona el siguiente contexto:

```python
eval_context = {
    "self": obj,                    # Objeto que llama (partner, journal, etc.)
    "float_repr": float_repr,       # Función de formateo
    "connection": connection,        # Conexión ARCA activa
    "company_id": company_id,       # Compañía actual
    "context_today": datetime.today,
    "datetime": datetime,
    "dateutil": dateutil,
    "relativedelta": relativedelta,
    "extra_values": kwargs.get("extra_values"),  # Valores adicionales
    "time": time,
    "ws_res": response,             # ⚠️ Objeto zeep de respuesta (NO es un dict)
}
```

### ⚠️ IMPORTANTE: ws_res es un objeto zeep, NO un diccionario

**REGLA CRÍTICA:** El objeto `ws_res` en el contexto de evaluación es un **objeto zeep** (resultado directo del servicio SOAP), **NO un diccionario Python**.

#### ❌ INCORRECTO (causará error):
```python
# NO usar .get() - zeep objects NO tienen método .get()
ws_res.get('return', {})
ws_res.get('personaReturn', {}).get('datosGenerales', {})
```

#### ✅ CORRECTO (sintaxis para objetos zeep):
```python
# Usar sintaxis de corchetes [] con validación condicional
ws_res["return"] if ws_res and "return" in ws_res else {}
ws_res["personaReturn"]["datosGenerales"] if ws_res and "personaReturn" in ws_res and "datosGenerales" in ws_res["personaReturn"] else {}
```

#### 📝 Explicación técnica:

1. **`call_arca_service()`** en `arcaws_connection.py` retorna el objeto zeep **sin serializar**
2. Los objetos zeep (de la librería `python-zeep`) **NO son diccionarios** y **NO tienen método `.get()`**
3. Para acceder a atributos zeep se debe usar:
   - **Sintaxis de corchetes**: `ws_res["campo"]`
   - **Operador `in`** para validar existencia: `"campo" in ws_res`
   - **Validación condicional**: `ws_res and "campo" in ws_res`

4. **Consistencia entre módulos**: El módulo `l10n_ar_fiscal_ws_fe` usa esta misma sintaxis:
   ```python
   # Ejemplo de l10n_ar_fiscal_ws_fe/data/arcaws.xml
   ws_res["FeDetResp"]["FECAEDetResponse"][0]["CAE"] if ws_res and "FeDetResp" in ws_res else False
   ```

#### 🔧 Alternativa avanzada (serialización):

Si se prefiere trabajar con diccionarios reales, se puede modificar `call_arca_service()` para serializar:

```python
from zeep.helpers import serialize_object

def call_arca_service(self, method_name, data, **kwargs):
    # ... código existente ...
    response = getattr(client.service, method_name)(**data, **kwargs)

    # Serializar objeto zeep a diccionario
    response_dict = serialize_object(response, target_cls=dict)

    # Agregar metadatos
    response_dict["xml_request"] = etree.tostring(...)
    response_dict["xml_response"] = etree.tostring(...)

    return response_dict
```

**⚠️ NOTA:** Si se implementa la serialización, habría que actualizar **todos** los `response_dict` en `l10n_ar_fiscal_ws_fe/data/arcaws.xml` para usar `.get()` en lugar de `[]`.

## Manejo automático de autenticación

La arquitectura maneja automáticamente:

1. **Token y Sign**: Obtenidos de `company_id.arca_get_connection(code)`
2. **CUIT**: Tomado de `company_id.partner_id.l10n_ar_vat`
3. **Estructura Auth**: Construida automáticamente en `connection.call_arca_service()`

**No es necesario:**
- Construir manualmente el diccionario Auth
- Manejar tokens expirados
- Gestionar conexiones

## Ejemplo completo de flujo

```
1. Usuario → Botón "Actualizar desde ARCA" en partner
2. res_partner.get_data_from_padron_arca()
3. → arcaws.search([code='ws_sr_padron_a5'])
4. → method_ids.filtered(name='get_persona')
5. → call_arca_method(obj=partner, extra_values={'cuit': '20123456789'})
6. → company.arca_get_connection('ws_sr_padron_a5')
7. → connection.call_arca_service('getPersona_v2', query_dict)
8. → AFIP Web Service
9. ← Respuesta persona
10. ← Parseo con parce_census_vals()
11. ← Retorno de vals para actualizar partner
```

## Definición de definition_dict y response_dict (Opcional)

Si necesitas definir la estructura de la petición y respuesta en XML:

### definition_dict - Estructura de la petición

```xml
<record id="ws_sr_padron_a5_get_persona" model="arcaws.method">
    <field name="name">get_persona</field>
    <field name="arcaws_id" ref="ws_sr_padron_a5"/>
    <field name="method_name">getPersona_v2</field>
    <field name="definition_dict">{
    'Auth': {
        'Token': connection.token,
        'Sign': connection.sign,
        'Cuit': int(company_id.partner_id.l10n_ar_vat or 0),
    },
    'idPersona': int(extra_values.get('cuit', 0)),
}</field>
</record>
```

### response_dict - Procesamiento de la respuesta

**⚠️ IMPORTANTE:** Usar sintaxis de corchetes `[]` para objetos zeep, NO `.get()`

```xml
<field name="response_dict">{
    'personaReturn': ws_res["personaReturn"] if ws_res and "personaReturn" in ws_res else {},
    'datosGenerales': ws_res["personaReturn"]["datosGenerales"] if ws_res and "personaReturn" in ws_res and "datosGenerales" in ws_res["personaReturn"] else {},
    'datosMonotributo': ws_res["personaReturn"]["datosMonotributo"] if ws_res and "personaReturn" in ws_res and "datosMonotributo" in ws_res["personaReturn"] else {},
    'datosRegimenGeneral': ws_res["personaReturn"]["datosRegimenGeneral"] if ws_res and "personaReturn" in ws_res and "datosRegimenGeneral" in ws_res["personaReturn"] else {},
    'errorConstancia': ws_res["personaReturn"]["errorConstancia"] if ws_res and "personaReturn" in ws_res and "errorConstancia" in ws_res["personaReturn"] else {},
    'errorMonotributo': ws_res["personaReturn"]["errorMonotributo"] if ws_res and "personaReturn" in ws_res and "errorMonotributo" in ws_res["personaReturn"] else {},
    'errorRegimenGeneral': ws_res["personaReturn"]["errorRegimenGeneral"] if ws_res and "personaReturn" in ws_res and "errorRegimenGeneral" in ws_res["personaReturn"] else {},
    'metadata': ws_res["personaReturn"]["metadata"] if ws_res and "personaReturn" in ws_res and "metadata" in ws_res["personaReturn"] else {},
}</field>
```

**Explicación de la sintaxis:**
- `ws_res["campo"]`: Acceso a atributo de objeto zeep (equivalente a `ws_res.campo` pero permite validación con `in`)
- `"campo" in ws_res`: Valida que el campo exista en el objeto zeep
- `ws_res and "campo" in ws_res`: Doble validación (objeto existe Y campo existe)
- **NO usar** `.get()`: Los objetos zeep no tienen este método

## Checklist de implementación

- [ ] Crear archivo XML de datos (`arcaws_padron_a5.xml`)
- [ ] Definir registro `arcaws` con URLs y código
- [ ] Definir métodos necesarios (`dummy`, `get_persona`)
- [ ] Actualizar modelo Python para usar `call_arca_method()`
- [ ] Agregar archivo XML al manifest
- [ ] Incrementar versión en manifest
- [ ] Manejar excepciones `ArcaError` correctamente
- [ ] Validar respuestas del servicio
- [ ] Probar en ambiente de homologación primero

## Estructura típica de respuesta del Padrón A5

```python
{
    'persona': {
        'idPersona': 20123456789,
        'tipoPersona': 'FISICA',  # o 'JURIDICA'
        'nombre': 'APELLIDO NOMBRE',
        'estadoClave': 'ACTIVO',
        'domicilioFiscal': {
            'direccion': 'CALLE 1234',
            'localidad': 'CIUDAD',
            'idProvincia': 1,
            'descripcionProvincia': 'CAPITAL FEDERAL',
            'codigoPostal': 'C1234ABC',
        },
        'impuesto': [
            {
                'idImpuesto': 30,
                'descripcionImpuesto': 'IVA',
                'periodo': 202401,
            }
        ],
        'actividad': [
            {
                'idActividad': 620100,
                'descripcionActividad': 'SERVICIOS INFORMATICOS',
                'periodo': 202401,
            }
        ],
        'categoriaMonotributo': {
            'idCategoria': 'H',
            'descripcionCategoria': 'CATEGORIA H',
            'periodo': 202401,
        } if es_monotributista else None,
    }
}
```

## Ejemplos de manejo de errores

### Error de CUIT no encontrado

```python
try:
    response = method_id.call_arca_method(obj=self, extra_values={'cuit': cuit})
except ArcaError as e:
    if 'No se encontro' in str(e) or 'not found' in str(e).lower():
        raise UserError(_("El CUIT %s no se encuentra registrado en AFIP") % cuit)
    raise
```

### Error de servicio no disponible

```python
try:
    response = method_id.call_arca_method(obj=self, extra_values={'cuit': cuit})
except Exception as e:
    if 'timeout' in str(e).lower() or 'connection' in str(e).lower():
        raise UserError(_(
            "El servicio de AFIP no está disponible en este momento.\n"
            "Por favor, intente nuevamente más tarde."
        ))
    raise
```

## Referencias

- **Modelo base**: `l10n_ar_fiscal_ws/models/arcaws.py`
- **Ejemplo de uso**: `l10n_ar_fiscal_ws_fe/models/account_journal.py`
- **Datos de facturación**: `l10n_ar_fiscal_ws_fe/data/arcaws.xml`
- **Documentación AFIP**: https://www.afip.gob.ar/ws/

## Notas adicionales

### Certificados necesarios

- El servicio Padrón A5 requiere certificado digital homologado por AFIP
- Mismo certificado que se usa para facturación electrónica
- Configurar en: Configuración → ARCA → Certificados

### Ambientes

- **Homologación**: Para pruebas y desarrollo
- **Producción**: Para operación real
- Cambiar en: Configuración → ARCA → Tipo de ambiente

### Frecuencia de consultas

- AFIP puede limitar la cantidad de consultas por minuto
- Implementar cache si es necesario para reducir llamadas
- Usar `last_update_census` para evitar consultas repetidas

---

**Fecha de creación:** 20/01/2026
**Versión Odoo:** 19.0
**Rama:** 19.0-mig-MAQ-work
**Autor:** Documentación técnica INGADHOC
