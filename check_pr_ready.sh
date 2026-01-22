#!/bin/bash

echo "🔍 Verificando si el PR está listo para enviar..."
echo ""

# 1. Verificar que estás en la rama correcta
BRANCH=$(git branch --show-current)
echo "📌 Rama actual: $BRANCH"

if [ "$BRANCH" != "19.0-mig-MAQ-work" ]; then
    echo "⚠️  No estás en 19.0-mig-MAQ-work"
    exit 1
fi

# 2. Verificar que no hay cambios sin commitear
echo "🔍 Verificando cambios sin commitear..."
if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "⚠️  Hay cambios sin commitear:"
    git status --short
    echo ""
    echo "   Puedes continuar, pero estos cambios no están incluidos en el check"
fi

# 3. Traer últimos cambios del remoto
echo "🔄 Verificando sincronización con remoto..."
git fetch capire-fork 2>/dev/null || echo "⚠️  No se pudo hacer fetch del remoto capire-fork"

# 4. Verificar si hay diferencias con el remoto
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse capire-fork/19.0-mig-MAQ-work 2>/dev/null)

if [ ! -z "$REMOTE" ] && [ "$LOCAL" != "$REMOTE" ]; then
    AHEAD=$(git rev-list capire-fork/19.0-mig-MAQ-work..HEAD --count)
    BEHIND=$(git rev-list HEAD..capire-fork/19.0-mig-MAQ-work --count)

    if [ "$BEHIND" -gt 0 ]; then
        echo "⚠️  Tu rama está $BEHIND commits atrás del remoto"
        echo "   Ejecuta: git pull capire-fork 19.0-mig-MAQ-work"
    fi

    if [ "$AHEAD" -gt 0 ]; then
        echo "✅ Tu rama está $AHEAD commits adelante (listos para push)"
    fi
fi

echo ""
echo "🔧 Ejecutando pre-commit hooks en archivos del PR..."
echo "   (Solo verificando archivos modificados en este PR)"
echo ""

# Obtener archivos modificados en este PR (comparando con la rama base)
BASE_BRANCH="b5c37e2"  # Último commit antes del PR
PR_FILES=$(git diff --name-only $BASE_BRANCH --diff-filter=d)

if [ -z "$PR_FILES" ]; then
    echo "⚠️  No se encontraron archivos modificados en el PR"
    exit 1
fi

python3 -m pre_commit run --files $PR_FILES

if [ $? -ne 0 ]; then
    echo ""
    echo "❌ Pre-commit falló. Corrige los errores y vuelve a ejecutar este script."
    exit 1
fi

# 6. Verificar bump de versión en manifest
echo ""
echo "📋 Verificando versión en manifest..."

# Comparar con la rama base del PR (19.0 si existe, sino 13.0)
BASE_BRANCH="origin/19.0"
git rev-parse --verify $BASE_BRANCH >/dev/null 2>&1
if [ $? -ne 0 ]; then
    BASE_BRANCH="origin/13.0"
fi

CHANGED_STRUCTURAL=$(git diff $BASE_BRANCH...HEAD --name-only 2>/dev/null | grep -E "(models|views|data|wizards|security)/")

if [ ! -z "$CHANGED_STRUCTURAL" ]; then
    echo "⚠️  Cambios estructurales detectados en:"
    echo "$CHANGED_STRUCTURAL" | head -5

    VERSION_CHANGED=$(git diff $BASE_BRANCH...HEAD -- "*/__manifest__.py" 2>/dev/null | grep -E "^\+.*['\"]version['\"]")

    if [ -z "$VERSION_CHANGED" ]; then
        echo ""
        echo "⚠️  ADVERTENCIA: Cambios estructurales detectados pero no se incrementó version en __manifest__.py"
        echo "   Considera incrementar la versión si:"
        echo "   - Agregaste/modificaste modelos, campos o vistas"
        echo "   - Modificaste datos XML, seguridad o reportes"
        echo ""
    else
        echo "✅ Versión incrementada detectada"
    fi
else
    echo "✅ No hay cambios estructurales que requieran bump de versión"
fi

# 7. Verificar que arcaws.xml es válido
echo ""
echo "📄 Validando archivos XML..."

if [ -f "l10n_ar_fiscal_ws/data/arcaws.xml" ]; then
    if command -v xmllint &> /dev/null; then
        if xmllint --noout l10n_ar_fiscal_ws/data/arcaws.xml 2>&1; then
            echo "✅ arcaws.xml es válido"
        else
            echo "❌ arcaws.xml tiene errores de sintaxis"
            exit 1
        fi
    else
        echo "⚠️  xmllint no está instalado, saltando validación XML detallada"
    fi
else
    echo "⚠️  No se encontró l10n_ar_fiscal_ws/data/arcaws.xml"
fi

# 8. Verificar arquitectura ARCAWS
echo ""
echo "🏗️  Verificando uso correcto de arquitectura ARCAWS..."

DIRECT_CALLS=$(git diff $BASE_BRANCH...HEAD 2>/dev/null | grep -E "^\+.*call_arca_service.*auth\s*=")

if [ ! -z "$DIRECT_CALLS" ]; then
    echo "⚠️  Se detectaron posibles llamadas directas a call_arca_service con parámetro auth:"
    echo "$DIRECT_CALLS"
    echo ""
    echo "   Recuerda: usa method_id.call_arca_method() en lugar de llamadas directas"
    echo ""
else
    echo "✅ No se detectaron llamadas directas problemáticas"
fi

# 9. Verificar que hay commits
echo ""
echo "📝 Verificando commits..."
COMMITS=$(git log $BASE_BRANCH..HEAD --oneline 2>/dev/null | wc -l | tr -d ' ')

if [ "$COMMITS" -eq 0 ]; then
    echo "⚠️  No hay commits nuevos respecto a la rama base"
    exit 1
else
    echo "✅ $COMMITS commit(s) listo(s) para enviar:"
    git log $BASE_BRANCH..HEAD --oneline 2>/dev/null | head -5
fi

# 10. Resumen final
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "✅ ¡PR LISTO PARA ENVIAR!"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "Próximos pasos:"
echo "  1. Si hay cambios sin commitear, haz:"
echo "     git add ."
echo "     git commit -m 'feat(l10n_ar_fiscal_ws): descripción'"
echo ""
echo "  2. Push al fork:"
echo "     git push capire-fork 19.0-mig-MAQ-work"
echo ""
echo "  3. Verificar CI/CD en GitHub:"
echo "     https://github.com/adhoc-dev/odoo-argentina-ce/pull/9/checks"
echo ""
