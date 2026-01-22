# Propuesta: Sistema de Control de Errores para módulos ARCA (AFIP Web Services)

**Fecha:** 21 de enero de 2026  
**Rama:** 19.0-mig-MAQ-work  
**Autor:** Sistema de desarrollo  
**Estado:** Propuesta pendiente de implementación  

---

## Contexto

Los módulos `l10n_ar_fiscal_ws*` interactúan con servicios SOAP de ARCA (AFIP) que pueden fallar por múltiples razones:
- Problemas de autenticación/autorización
- Errores de conectividad
- Datos inválidos
- Límites de cuota excedidos
- Servicios fuera de línea

Actualmente, los errores se propagan sin contexto claro para el usuario final.

---

## Objetivos

1. **Capturar errores de forma centralizada** en la arquitectura ARCAWS
2. **Clasificar errores** por tipo (autenticación, red, validación, etc.)
3. **Proveer mensajes claros** al usuario según el error
4. **Registrar logs estructurados** para debugging
5. **Permitir reintento automático** cuando sea apropiado
6. **Mantener compatibilidad** con código existente

---

## Arquitectura propuesta

### 1. Jerarquía de excepciones personalizadas

Crear archivo `l10n_ar_fiscal_ws/exceptions.py`:

```python
"""Excepciones personalizadas para servicios ARCA."""

from odoo.exceptions import UserError


class ArcaError(UserError):
    """Error base para todos los errores de servicios ARCA."""
    
    def __init__(self, message, details=None, retry_after=None):
        """
        Args:
            message: Mensaje principal del error
            details: Detalles técnicos adicionales (opcional)
            retry_after: Segundos a esperar antes de reintentar (opcional)
        """
        self.details = details or {}
        self.retry_after = retry_after
        
        # Construir mensaje completo
        full_message = message
        if details:
            full_message += f"\n\nDetalles técnicos:\n{details}"
        
        super().__init__(full_message)


class ArcaAuthenticationError(ArcaError):
    """Error de autenticación con ARCA."""
    
    def __init__(self, message="Error de autenticación con ARCA", **kwargs):
        super().__init__(message, **kwargs)


class ArcaAuthorizationError(ArcaError):
    """Error de autorización (permisos insuficientes)."""
    
    def __init__(self, message="Computador/CUIT no autorizado para este servicio", **kwargs):
        super().__init__(message, **kwargs)


class ArcaConnectionError(ArcaError):
    """Error de conexión con servicios ARCA."""
    
    def __init__(self, message="No se pudo conectar con los servicios de ARCA", **kwargs):
        kwargs.setdefault('retry_after', 60)  # Reintentar después de 1 minuto
        super().__init__(message, **kwargs)


class ArcaValidationError(ArcaError):
    """Error de validación de datos enviados a ARCA."""
    
    def __init__(self, message="Los datos enviados a ARCA son inválidos", **kwargs):
        super().__init__(message, **kwargs)


class ArcaServiceError(ArcaError):
    """Error interno del servicio ARCA."""
    
    def __init__(self, message="Error interno en los servicios de ARCA", **kwargs):
        kwargs.setdefault('retry_after', 300)  # Reintentar después de 5 minutos
        super().__init__(message, **kwargs)


class ArcaQuotaExceededError(ArcaError):
    """Límite de consultas excedido."""
    
    def __init__(self, message="Se excedió el límite de consultas a ARCA", **kwargs):
        kwargs.setdefault('retry_after', 3600)  # Reintentar después de 1 hora
        super().__init__(message, **kwargs)
```

---

### 2. Handler centralizado de errores

Modificar `l10n_ar_fiscal_ws/models/arcaws_connection.py`:

```python
"""Handler centralizado de errores para conexiones ARCA."""

import logging
from zeep.exceptions import Fault, TransportError
from requests.exceptions import RequestException, Timeout, ConnectionError as RequestsConnectionError

from odoo import _, models
from ..exceptions import (
    ArcaError,
    ArcaAuthenticationError,
    ArcaAuthorizationError,
    ArcaConnectionError,
    ArcaValidationError,
    ArcaServiceError,
    ArcaQuotaExceededError,
)

_logger = logging.getLogger(__name__)


class ArcaErrorHandler:
    """Maneja y traduce errores de servicios ARCA."""
    
    # Patrones de errores conocidos
    ERROR_PATTERNS = {
        'authentication': [
            'token invalido',
            'ticket invalido',
            'token expirado',
            'ticket expirado',
        ],
        'authorization': [
            'computador no autorizado',
            'cuit no autorizado',
            'no tiene permiso',
            'acceso denegado',
        ],
        'validation': [
            'dato invalido',
            'formato incorrecto',
            'campo requerido',
            'valor fuera de rango',
        ],
        'quota': [
            'limite excedido',
            'cuota superada',
            'demasiadas consultas',
        ],
        'service': [
            'servicio no disponible',
            'error interno',
            'timeout',
        ],
    }
    
    @classmethod
    def handle_zeep_fault(cls, fault, context=None):
        """
        Traduce un Fault de Zeep a una excepción ARCA apropiada.
        
        Args:
            fault: zeep.exceptions.Fault
            context: Información de contexto (servicio, método, parámetros)
        
        Raises:
            ArcaError o subclase apropiada
        """
        message = str(fault).lower()
        
        # Clasificar por tipo de error
        error_type = cls._classify_error(message)
        
        # Construir detalles
        details = {
            'error_original': str(fault),
            'tipo': error_type,
        }
        if context:
            details.update(context)
        
        # Crear excepción apropiada
        if error_type == 'authentication':
            raise ArcaAuthenticationError(
                _("Error de autenticación con ARCA. "
                  "Verifique la configuración del certificado y credenciales."),
                details=details
            )
        
        elif error_type == 'authorization':
            raise ArcaAuthorizationError(
                _("Su computador o CUIT no está autorizado para acceder a este servicio de ARCA. "
                  "Contacte a AFIP para solicitar habilitación."),
                details=details
            )
        
        elif error_type == 'validation':
            raise ArcaValidationError(
                _("Los datos enviados a ARCA son inválidos: %s") % str(fault),
                details=details
            )
        
        elif error_type == 'quota':
            raise ArcaQuotaExceededError(
                _("Se excedió el límite de consultas permitidas. "
                  "Intente nuevamente más tarde."),
                details=details
            )
        
        elif error_type == 'service':
            raise ArcaServiceError(
                _("Los servicios de ARCA están experimentando problemas. "
                  "Intente nuevamente más tarde."),
                details=details
            )
        
        else:
            # Error genérico
            raise ArcaError(
                _("Error al comunicarse con ARCA: %s") % str(fault),
                details=details
            )
    
    @classmethod
    def handle_connection_error(cls, error, context=None):
        """
        Maneja errores de conexión/transporte.
        
        Args:
            error: Exception (TransportError, RequestException, etc.)
            context: Información de contexto
        
        Raises:
            ArcaConnectionError
        """
        details = {
            'error_original': str(error),
            'tipo': 'connection',
        }
        if context:
            details.update(context)
        
        raise ArcaConnectionError(
            _("No se pudo conectar con los servicios de ARCA. "
              "Verifique su conexión a internet y que los servicios estén disponibles."),
            details=details
        )
    
    @classmethod
    def _classify_error(cls, message):
        """Clasifica un mensaje de error según patrones conocidos."""
        message_lower = message.lower()
        
        for error_type, patterns in cls.ERROR_PATTERNS.items():
            if any(pattern in message_lower for pattern in patterns):
                return error_type
        
        return 'unknown'


class ArcawsConnection(models.Model):
    _inherit = "arcaws.connection"
    
    def call_arca_service(self, method_name, data=None, **kwargs):
        """Wrapper con manejo de errores para llamadas a servicios ARCA."""
        context = {
            'service': self.arcaws_id.code,
            'method': method_name,
            'company': self.company_id.name,
            'environment': self.arcaws_id.environment_type,
        }
        
        try:
            # Llamada original
            return super().call_arca_service(method_name, data, **kwargs)
            
        except Fault as fault:
            # Error SOAP
            _logger.error(
                "ARCA Fault - Service: %s, Method: %s, Error: %s",
                context['service'],
                context['method'],
                str(fault),
                exc_info=True
            )
            ArcaErrorHandler.handle_zeep_fault(fault, context)
            
        except (TransportError, RequestsConnectionError, Timeout) as conn_error:
            # Error de conexión
            _logger.error(
                "ARCA Connection Error - Service: %s, Method: %s, Error: %s",
                context['service'],
                context['method'],
                str(conn_error),
                exc_info=True
            )
            ArcaErrorHandler.handle_connection_error(conn_error, context)
            
        except Exception as e:
            # Error inesperado
            _logger.exception(
                "ARCA Unexpected Error - Service: %s, Method: %s",
                context['service'],
                context['method']
            )
            raise ArcaError(
                _("Error inesperado al comunicarse con ARCA: %s") % str(e),
                details=context
            )
```

---

### 3. Actualizar `__init__.py` del módulo

```python
# l10n_ar_fiscal_ws/__init__.py
from . import models
from . import wizard
from . import exceptions  # ← Agregar
```

---

### 4. Adaptar métodos existentes

#### En `res_company.py` - método `_arca_create_connection`:

```python
def _arca_create_connection(self, arcaws, environment_type):
    """Crea una nueva conexión ARCA con manejo de errores."""
    from ..exceptions import ArcaAuthenticationError, ArcaAuthorizationError, ArcaConnectionError
    from zeep.exceptions import Fault, TransportError
    from requests.exceptions import ConnectionError as RequestsConnectionError
    
    self.ensure_one()
    
    try:
        # ... código existente de creación de conexión ...
        response = getattr(client.service, "loginCms")(sign_tra)
        
    except Fault as fault:
        if 'computador no autorizado' in str(fault).lower():
            raise ArcaAuthorizationError(
                _("Su computador no está autorizado para acceder a los servicios de ARCA.\n\n"
                  "Debe solicitar la habilitación del servicio '%s' en AFIP:\n"
                  "1. Ingresar a https://www.afip.gob.ar con Clave Fiscal\n"
                  "2. Ir a 'Sistema Registral' > 'Administración de Relaciones'\n"
                  "3. Habilitar el servicio para su CUIT\n"
                  "4. Autorizar el certificado digital") % arcaws.name,
                details={
                    'service': arcaws.code,
                    'company': self.name,
                    'cuit': self.partner_id.vat,
                }
            )
        else:
            # Delegar al handler
            ArcaErrorHandler.handle_zeep_fault(fault, {
                'service': arcaws.code,
                'method': 'loginCms',
                'company': self.name,
            })
    
    except (TransportError, RequestsConnectionError) as conn_error:
        raise ArcaConnectionError(
            _("No se pudo conectar con ARCA para autenticar.\n"
              "Verifique:\n"
              "• Conexión a internet\n"
              "• URL del servicio: %s\n"
              "• Firewall/proxy") % arcaws.url,
            details={
                'service': arcaws.code,
                'url': arcaws.url,
                'error': str(conn_error),
            }
        )
    
    # ... resto del código ...
```

#### En `res_partner.py` - método `get_data_from_padron_arca`:

```python
def get_data_from_padron_arca(self):
    """Obtiene datos del padrón ARCA con manejo de errores."""
    from ..exceptions import ArcaError
    
    self.ensure_one()
    
    try:
        cuit = self.ensure_vat()
        arcaws = self.env["arcaws"].search([("code", "=", "ws_sr_constancia_inscripcion")])
        
        if not arcaws:
            raise UserError(_("No se encontró configurado el servicio de Padrón ARCA"))
        
        method_id = arcaws.method_ids.filtered(lambda m: m.name == "get_persona")
        if not method_id:
            raise UserError(_("No se encontró el método 'get_persona' configurado"))
        
        # Llamar al servicio (manejo de errores en call_arca_method)
        res = method_id.call_arca_method(obj=self, extra_values={"cuit": cuit})
        
        if not res:
            raise UserError(_("ARCA no devolvió información para el CUIT %s") % cuit)
        
        # Procesar respuesta
        vals = self.parce_census_vals(res)
        return vals
        
    except ArcaError:
        # Re-lanzar errores ARCA tal como vienen (ya tienen buen mensaje)
        raise
    
    except Exception as e:
        # Capturar cualquier otro error y contextualizarlo
        _logger.exception("Error inesperado al consultar padrón ARCA para %s", self.name)
        raise UserError(
            _("Error al consultar el padrón ARCA para %s:\n%s") % (self.name, str(e))
        )
```

#### En `account_move.py` - método `do_pyafipws_request_cae`:

```python
def do_pyafipws_request_cae(self):
    """Solicita CAE a AFIP con manejo de errores mejorado."""
    from ..exceptions import ArcaError
    
    # ... código existente ...
    
    msg = False
    response = {}
    
    try:
        method_id = arcaws.method_ids.filtered(
            lambda m: m.name == "request_invoice_authorization"
        )
        if not method_id:
            msg = _("No se encontró el método 'request_invoice_authorization' configurado")
        else:
            response = method_id.call_arca_method(
                obj=inv,
                mode="exec",
                extra_values={
                    "next_invoice_number": next_invoice_number,
                    "amounts": amounts,
                    "arca_document_code": arca_document_code,
                },
            )
            
    except ArcaError as arca_err:
        # Error ARCA con contexto
        msg = str(arca_err)
        if hasattr(arca_err, 'details'):
            response = arca_err.details
        
    except Exception as e:
        # Error inesperado
        msg = _("Error inesperado al solicitar CAE: %s") % str(e)
        _logger.exception("Error al solicitar CAE para factura %s", inv.name)
    
    if msg:
        xml_request = response.get("afip_xml_request", "N/A")
        xml_response = response.get("afip_xml_response", "N/A")
        
        _logger.error(
            "AFIP Validation Error: %s\nXML Request: %s\nXML Response: %s",
            msg, xml_request, xml_response
        )
        
        raise UserError(_("Error de validación AFIP:\n\n%s") % msg)
    
    # ... resto del código ...
```

---

### 5. Agregar retry automático (opcional)

Crear mixin `l10n_ar_fiscal_ws/models/arca_retry_mixin.py`:

```python
"""Mixin para reintentos automáticos en servicios ARCA."""

import time
import logging
from functools import wraps

from odoo import models
from ..exceptions import ArcaConnectionError, ArcaServiceError

_logger = logging.getLogger(__name__)


def arca_retry(max_attempts=3, backoff_factor=2):
    """
    Decorador para reintentar automáticamente llamadas a ARCA.
    
    Args:
        max_attempts: Número máximo de intentos
        backoff_factor: Factor de multiplicación para el delay entre intentos
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            attempt = 1
            delay = 1
            
            while attempt <= max_attempts:
                try:
                    return func(*args, **kwargs)
                    
                except (ArcaConnectionError, ArcaServiceError) as e:
                    if attempt == max_attempts:
                        # Último intento fallido
                        _logger.error(
                            "Fallo definitivo después de %d intentos: %s",
                            max_attempts, str(e)
                        )
                        raise
                    
                    # Calcular delay con backoff exponencial
                    if hasattr(e, 'retry_after') and e.retry_after:
                        delay = e.retry_after
                    else:
                        delay = delay * backoff_factor
                    
                    _logger.warning(
                        "Intento %d/%d falló: %s. Reintentando en %d segundos...",
                        attempt, max_attempts, str(e), delay
                    )
                    
                    time.sleep(delay)
                    attempt += 1
            
        return wrapper
    return decorator


class ArcaRetryMixin(models.AbstractModel):
    """Mixin para agregar capacidades de retry a métodos."""
    
    _name = 'arca.retry.mixin'
    _description = 'ARCA Retry Mixin'
    
    @arca_retry(max_attempts=3, backoff_factor=2)
    def _arca_call_with_retry(self, method_id, **call_kwargs):
        """
        Llama a un método ARCA con reintentos automáticos.
        
        Args:
            method_id: arcaws.method record
            **call_kwargs: Argumentos para call_arca_method
        
        Returns:
            Resultado de la llamada
        """
        return method_id.call_arca_method(**call_kwargs)
```

**Uso:**

```python
class ResPartner(models.Model):
    _inherit = ['res.partner', 'arca.retry.mixin']
    
    def get_data_from_padron_arca(self):
        # ...
        
        # Llamada con retry automático
        res = self._arca_call_with_retry(
            method_id,
            obj=self,
            extra_values={"cuit": cuit}
        )
```

---

### 6. Dashboard de errores (opcional avanzado)

Crear modelo para tracking de errores `l10n_ar_fiscal_ws/models/arca_error_log.py`:

```python
"""Log de errores de servicios ARCA."""

from odoo import fields, models


class ArcaErrorLog(models.Model):
    _name = 'arca.error.log'
    _description = 'ARCA Error Log'
    _order = 'create_date desc'
    _rec_name = 'service_code'
    
    service_code = fields.Char('Servicio', required=True, index=True)
    method_name = fields.Char('Método', index=True)
    error_type = fields.Selection([
        ('authentication', 'Autenticación'),
        ('authorization', 'Autorización'),
        ('connection', 'Conexión'),
        ('validation', 'Validación'),
        ('service', 'Servicio'),
        ('quota', 'Cuota excedida'),
        ('unknown', 'Desconocido'),
    ], string='Tipo de Error', required=True, index=True)
    
    error_message = fields.Text('Mensaje de Error', required=True)
    error_details = fields.Text('Detalles Técnicos')
    
    company_id = fields.Many2one('res.company', 'Compañía', index=True)
    user_id = fields.Many2one('res.users', 'Usuario', default=lambda self: self.env.user)
    
    resolved = fields.Boolean('Resuelto', default=False)
    resolution_notes = fields.Text('Notas de Resolución')
```

Modificar `ArcaErrorHandler` para registrar errores:

```python
@classmethod
def log_error(cls, env, error_type, service_code, method_name, message, details=None, company_id=None):
    """Registra un error en el log."""
    env['arca.error.log'].sudo().create({
        'service_code': service_code,
        'method_name': method_name,
        'error_type': error_type,
        'error_message': message,
        'error_details': str(details) if details else False,
        'company_id': company_id.id if company_id else False,
    })
```

---

## Plan de implementación

### Fase 1: Fundamentos (Prioritario)
- [ ] Crear `exceptions.py` con jerarquía de excepciones
- [ ] Implementar `ArcaErrorHandler` en `arcaws_connection.py`
- [ ] Actualizar `__init__.py`
- [ ] **Bump de versión en `__manifest__.py`**: `19.0.1.3.0` → `19.0.1.4.0`

### Fase 2: Adaptar código existente
- [ ] Modificar `_arca_create_connection` en `res_company.py`
- [ ] Modificar `call_arca_service` en `arcaws_connection.py`
- [ ] Actualizar `get_data_from_padron_arca` en `res_partner.py`
- [ ] Actualizar `do_pyafipws_request_cae` en `account_move.py`
- [ ] **Bump de versión en manifests afectados**

### Fase 3: Features avanzadas (Opcional)
- [ ] Implementar `arca_retry_mixin.py`
- [ ] Crear modelo `arca.error.log`
- [ ] Agregar vistas para dashboard de errores
- [ ] Crear reportes/métricas de errores
- [ ] **Bump de versión**

### Fase 4: Documentación y testing
- [ ] Documentar uso de excepciones
- [ ] Crear tests unitarios para error handling
- [ ] Documentar procedimientos de resolución de errores comunes
- [ ] Actualizar README del módulo

---

## Beneficios

✅ **Mensajes claros**: Usuarios entienden qué pasó y qué hacer  
✅ **Debugging facilitado**: Logs estructurados con contexto completo  
✅ **Resiliencia**: Reintentos automáticos para errores transitorios  
✅ **Monitoreo**: Dashboard para identificar patrones de errores  
✅ **Mantenibilidad**: Un solo punto para gestionar errores ARCA  
✅ **Compatibilidad**: No rompe código existente  

---

## Ejemplo de uso final

```python
# El código de aplicación queda simple
def action_validate_invoice(self):
    try:
        self.do_pyafipws_request_cae()
    except ArcaAuthorizationError as e:
        # Error específico con mensaje claro
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Error de Autorización ARCA',
                'message': str(e),
                'type': 'danger',
                'sticky': True,
            }
        }
    except ArcaError as e:
        # Cualquier otro error ARCA
        _logger.error("Error ARCA al validar factura %s: %s", self.name, e)
        raise  # Re-lanzar con mensaje claro
```

---

## Errores conocidos que resuelve esta propuesta

### Error actual: "UnboundLocalError: cannot access local variable 'response'"

**Ubicación:** `l10n_ar_fiscal_ws_fe/models/account_move.py` línea 268

**Causa:** La variable `response` no está inicializada cuando ocurre una excepción antes de su asignación.

**Solución con esta propuesta:**
```python
response = {}  # Inicializar al principio
try:
    response = method_id.call_arca_method(...)
except ArcaError as e:
    # response ya existe, puede usarse en logs
    xml_request = response.get("afip_xml_request", "N/A")
```

### Error actual: "Computador no autorizado a acceder al servicio"

**Ubicación:** `l10n_ar_fiscal_ws/models/res_company.py` en `_arca_create_connection`

**Causa:** Zeep lanza `Fault` genérico sin contexto claro para el usuario.

**Solución con esta propuesta:**
```python
except Fault as fault:
    if 'computador no autorizado' in str(fault).lower():
        raise ArcaAuthorizationError(
            _("Su computador no está autorizado...\n"
              "Pasos para habilitar:\n1. ...\n2. ...\n3. ..."),
            details={'service': arcaws.code, 'cuit': self.partner_id.vat}
        )
```

---

## Notas de implementación

### Compatibilidad con código existente

La propuesta es **backwards-compatible**:
- Todas las nuevas excepciones heredan de `UserError`
- El código existente seguirá funcionando sin cambios
- Los cambios son incrementales y pueden aplicarse gradualmente

### Testing

Crear tests en `l10n_ar_fiscal_ws/tests/test_arca_error_handling.py`:

```python
from odoo.tests import TransactionCase
from ..exceptions import (
    ArcaAuthenticationError,
    ArcaAuthorizationError,
    ArcaConnectionError,
)


class TestArcaErrorHandling(TransactionCase):
    
    def test_authentication_error_has_details(self):
        """Verificar que los errores incluyen detalles técnicos."""
        details = {'service': 'wsfe', 'method': 'FECAESolicitar'}
        
        with self.assertRaises(ArcaAuthenticationError) as cm:
            raise ArcaAuthenticationError(
                "Token inválido",
                details=details
            )
        
        error = cm.exception
        self.assertIn('service', error.details)
        self.assertEqual(error.details['service'], 'wsfe')
    
    def test_retry_after_in_connection_error(self):
        """Verificar que errores de conexión incluyen retry_after."""
        error = ArcaConnectionError()
        self.assertIsNotNone(error.retry_after)
        self.assertEqual(error.retry_after, 60)
```

---

## Referencias

- **Odoo 19 Exception Handling**: https://www.odoo.com/documentation/19.0/developer/reference/backend/exceptions.html
- **Zeep Documentation**: https://docs.python-zeep.org/en/master/
- **AFIP Web Services**: https://www.afip.gob.ar/ws/

---

## Changelog

- **2026-01-21**: Propuesta inicial creada
- **Pendiente**: Implementación Fase 1
