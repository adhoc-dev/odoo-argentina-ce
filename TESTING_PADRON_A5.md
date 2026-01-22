# Pruebas para Padrón A5 (sin usar action_dummie)

## ✅ Prueba 1: Verificar configuración del servicio

```python
# En Odoo shell
arcaws = env['arcaws'].search([('code', '=', 'ws_sr_constancia_inscripcion')], limit=1)
print(f"✓ Servicio encontrado: {arcaws.name}")
print(f"✓ URL homologación: {arcaws.homologation_url}")
print(f"✓ Métodos: {arcaws.method_ids.mapped('name')}")

# Verificar método get_persona
method = arcaws.method_ids.filtered(lambda m: m.name == 'get_persona')
method.ensure_one()
print(f"✓ Método get_persona OK: {method.method_name}")
```

## ✅ Prueba 2: Consulta real a ARCA

```python
# Probar con un partner que tenga CUIT válido
partner = env['res.partner'].search([('vat', '!=', False)], limit=1)
print(f"Probando con: {partner.name} - CUIT: {partner.vat}")

# Ejecutar consulta
try:
    vals = partner.get_data_from_padron_arca()
    print("✅ CONSULTA EXITOSA")
    print(f"Datos recibidos: {list(vals.keys())}")
    print(f"Nombre: {vals.get('name')}")
    print(f"Domicilio: {vals.get('street')}")
    print(f"Ciudad: {vals.get('city')}")
    print(f"Provincia: {vals.get('state_id')}")
except Exception as e:
    print(f"❌ ERROR: {e}")
```

## ✅ Prueba 3: Verificar lógica de provincias

```python
# Caso A: Simular respuesta ARCA para CABA sin localidad
partner = env['res.partner'].create({
    'name': 'Test CABA',
    'vat': 'AR20123456789',
})

# Simular datos de ARCA
census_mock = {
    'denominacion': 'TEST CABA SA',
    'direccion': 'AV CORRIENTES 1234',
    'localidad': '',  # Vacío
    'provincia': 'CAPITAL FEDERAL',  # Debe detectar CABA
    'cod_postal': 'C1043AAZ',
    'imp_iva': 'S',
    'monotributo': 'N',
}

vals = partner.parse_census_vals(census_mock)
print(f"✓ Provincia asignada: {env['res.country.state'].browse(vals['state_id']).name if vals.get('state_id') else 'No asignada'}")
print(f"✓ Ciudad: {vals.get('city')}")

# Caso B: Provincia sin localidad (NO debe ser CABA)
census_mock2 = {
    'denominacion': 'TEST CORDOBA SA',
    'direccion': 'CALLE 456',
    'localidad': '',  # Vacío
    'provincia': 'CORDOBA',  # NO es CABA
    'cod_postal': '5000',
    'imp_iva': 'S',
    'monotributo': 'N',
}

vals2 = partner.parse_census_vals(census_mock2)
state = env['res.country.state'].browse(vals2['state_id']) if vals2.get('state_id') else None
print(f"✓ Provincia asignada: {state.name if state else 'No asignada'}")
print(f"✓ NO debe ser CABA: {state.code not in ['C', 'CABA', 'ABA'] if state else 'OK'}")
```

## ✅ Prueba 4: Desde la interfaz (UI)

1. Ir a **Contactos**
2. Crear o abrir un partner con CUIT argentino válido
3. Presionar botón **"Actualizar desde Padrón ARCA"**
4. Verificar:
   - ✓ Aparece notificación de éxito
   - ✓ Los datos se actualizan (nombre, dirección, provincia)
   - ✓ El campo `last_update_census` se actualiza con la fecha de hoy

## ✅ Prueba 5: Logs de debug

En la terminal donde corre Odoo:

```bash
docker logs -f odoo19dev-web 2>&1 | grep -E "(ARCA|getPersona|parse_census)"
```

Deberías ver:
```
DEBUG ... === Respuesta ARCA completa para CUIT ...
DEBUG ... nombre: ...
DEBUG ... apellido: ...
DEBUG ... provincia: ...
DEBUG ... localidad: ...
```

## 🚫 NO necesitas probar:

- ❌ `action_dummie()` - Es para otros servicios ARCA
- ❌ Métodos dummy - No son necesarios para Padrón A5
- ❌ Conexiones manuales - La arquitectura ARCAWS lo maneja

## 📊 Checklist mínimo

- [ ] Servicio `ws_sr_constancia_inscripcion` existe
- [ ] Método `get_persona` existe y es único
- [ ] Consulta a ARCA retorna datos válidos
- [ ] Provincias se asignan correctamente (CABA detectado solo cuando corresponde)
- [ ] UI actualiza datos sin errores
- [ ] Logs muestran información detallada en modo debug

## ⚡ Prueba rápida (30 segundos)

```bash
# Acceder a Odoo shell
docker exec -it odoo19dev-web odoo shell -d TU_BD

# Ejecutar
partner = env['res.partner'].search([('vat', '!=', False)], limit=1)
vals = partner.get_data_from_padron_arca()
print("✅ TODO OK" if vals else "❌ ERROR")
```
