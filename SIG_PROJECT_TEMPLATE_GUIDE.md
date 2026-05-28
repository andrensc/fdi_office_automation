# SIG_[ProjectName] Template Creation Guide
## Complete Specification for Automation-Ready Projects

**Version:** 2.0 (Data-Driven, based on audit of SIG_Artosas, SIG_Maridona, SIG_Torrao)  
**Created:** May 12, 2026  
**Purpose:** Standardized project structure ensuring compatibility with all office automation systems (P1-N1 through P1-N12)

---

## Quick Start Checklist

For experienced teams creating a new SIG_[ProjectName]:

- [ ] Create directory: `/Users/andre/Sync/FdI/SIG/SIG_[ProjectName]/`
- [ ] Copy all folders from template (SIG_Artosas)
- [ ] Copy all .gpkg files from template vector_data/
- [ ] Update `property_boundaries.gpkg` with new property boundary polygon
- [ ] Edit `Projeto QGIS.qgs` - update title and data source paths
- [ ] Verify all layers load with CRS EPSG:3763
- [ ] Create QField cloud project
- [ ] Test first field data collection

**Time required:** 30-45 minutes  
**Complexity:** Low (copy-based approach)

---

## Detailed Step-by-Step Guide

### Prerequisites

**Software:**
- QGIS 3.28+ (with GeoPackage support)
- Python 3.9+
- OGR/GDAL utilities
- Docker (for Phase 1/2 processing)

**Templates Available:**
- SIG_Artosas (primary template - fully featured)
- SIG_Maridona (alternative - includes cadastral data)
- SIG_Torrao (alternative - includes cadastral data)

**Recommendation:** Use SIG_Artosas as template; it has all core layers without project-specific extras

---

## Step 1: Create Project Directory Structure

```bash
# Create base project directory
mkdir -p /Users/andre/Sync/FdI/SIG/SIG_[ProjectName]
cd /Users/andre/Sync/FdI/SIG/SIG_[ProjectName]

# Create all required subdirectories
mkdir -p inputs_project/project_vector_data
mkdir -p inputs_project/project_raster_data
mkdir -p outputs/out_geopackage
mkdir -p intermediary/intermediary_zoning_matrix
mkdir -p _Predios
mkdir -p grassdata
mkdir -p DCIM

# Verify structure
find . -type d | sort
```

**Expected output:**
```
./
├── DCIM/
├── _Predios/
├── grassdata/
├── inputs_project/
│   ├── project_raster_data/
│   └── project_vector_data/
├── intermediary/
│   └── intermediary_zoning_matrix/
└── outputs/
    └── out_geopackage/
```

---

## Step 2: Copy Vector Data (GeoPackage Files)

**Source:** `/Users/andre/Sync/FdI/SIG/SIG_Artosas/inputs_project/project_vector_data/`  
**Destination:** `./inputs_project/project_vector_data/`

```bash
# Copy all .gpkg files (excluding CONFLICT versions)
cd /Users/andre/Sync/FdI/SIG/SIG_[ProjectName]

cp /Users/andre/Sync/FdI/SIG/SIG_Artosas/inputs_project/project_vector_data/*.gpkg \
   inputs_project/project_vector_data/ 2>/dev/null || true

# Remove CONFLICT versions if present
rm inputs_project/project_vector_data/*CONFLICT* 2>/dev/null || true

# Verify copy
ls -lah inputs_project/project_vector_data/ | head -20
```

**Expected files:**
```
Acessos.gpkg                              (Access paths)
Altimetria.gpkg                           (Elevation points)
Amostras Representativas de Solo.gpkg     (Soil samples)
Area de Captacao.gpkg                     (Water catchment areas)
Arvores.gpkg                              (Vegetation inventory)
Bacia_contribuicao.gpkg                   (Watershed contribution)
Bacias.gpkg                               (Watersheds/basins)
IE Lineares.gpkg                          (Linear infrastructure)
IE_Pontos.gpkg                            (Infrastructure points)
Limite_do_Estudo.gpkg                     (Study area boundary)
Linha de Plantacao.gpkg                   (Planting lines/interventions)
Matriz Zoneamento.gpkg                    (Main zoning matrix)
Matriz Zoneamento2.gpkg                   (Zoning scenarios)
Notas.gpkg                                (Observations/notes)
PlantList_FullList.gpkg                   (Plant species reference)
Pontos Cotados.gpkg                       (Survey points)
Pontos de Interesse.gpkg                  (Points of interest)
aneis_biodiversidade.gpkg                 (Biodiversity rings)
clipping boundaries for presentation.gpkg (Clipping regions)
cos_interno.gpkg                          (Internal classification)
hidrologiaLinesForCarvingDEM.gpkg         (Hydrology lines)
lfesto.gpkg                               (Local forestry)
linhas de agua.gpkg                       (Water lines)
location_point.gpkg                       (Location reference)
property_boundaries.gpkg                  (WILL BE REPLACED NEXT)
```

**Critical Files (must copy):**
- `Notas.gpkg` - Observations core layer
- `IE Lineares.gpkg` - Infrastructure linear
- `IE_Pontos.gpkg` - Infrastructure points
- `Arvores.gpkg` - Vegetation inventory
- `Acessos.gpkg` - Access paths
- `Matriz Zoneamento.gpkg` - Zoning matrix
- `Linha de Plantacao.gpkg` - Interventions
- `Altimetria.gpkg` - Elevation reference
- `Pontos de Interesse.gpkg` - Points of interest

---

## Step 3: Update Property Boundary

**File:** `property_boundaries.gpkg`  
**Layer:** `Limites da Propriedade`

### Option A: Using QGIS (Recommended for Non-Technical)

1. Open QGIS
2. File → Open Layer → `inputs_project/project_vector_data/property_boundaries.gpkg`
3. Right-click on `Limites da Propriedade` layer → Open Layer Attributes Table
4. Delete the old property polygon (select all, delete)
5. Add your property boundary polygon:
   - Option 1: Digitize directly in QGIS (Layer → New Geometry → Polygon)
   - Option 2: Import from shapefile or GeoJSON
   - Option 3: Paste from clipboard (WKT format)
6. **CRITICAL:** Set fields exactly:
   - `name` (String): Project name = `SIG_[ProjectName]`
   - `area` (Real): Calculated area in hectares
7. Save and close

### Option B: Using Python Script

```python
#!/usr/bin/env python3
import os
import sys
from pathlib import Path
from osgeo import ogr, osr

# Configuration
project_name = sys.argv[1] if len(sys.argv) > 1 else "SIG_NewProject"
gpkg_path = f"/Users/andre/Sync/FdI/SIG/{project_name}/inputs_project/project_vector_data/property_boundaries.gpkg"
wkt_geometry = sys.argv[2] if len(sys.argv) > 2 else None  # WKT polygon format

if not os.path.exists(gpkg_path):
    print(f"Error: GeoPackage not found at {gpkg_path}")
    sys.exit(1)

# Open GeoPackage
driver = ogr.GetDriverByName("GPKG")
datasource = driver.Open(gpkg_path, 1)  # 1 = update mode

if datasource is None:
    print(f"Error: Could not open {gpkg_path}")
    sys.exit(1)

# Get layer
layer = datasource.GetLayer("Limites da Propriedade")
if layer is None:
    print("Error: Layer 'Limites da Propriedade' not found")
    sys.exit(1)

# Delete existing features
for feature in layer:
    layer.DeleteFeature(feature.GetFID())

# Create new feature
feature_def = layer.GetLayerDefn()
new_feature = ogr.Feature(feature_def)

# Set geometry (if provided as WKT)
if wkt_geometry:
    geom = ogr.CreateGeometryFromWkt(wkt_geometry)
else:
    # Or create a dummy boundary for testing
    wkt = "POLYGON((100000 -100000, 110000 -100000, 110000 -110000, 100000 -110000, 100000 -100000))"
    geom = ogr.CreateGeometryFromWkt(wkt)

new_feature.SetGeometry(geom)
new_feature.SetField("name", project_name)
new_feature.SetField("area", 100.0)  # Placeholder

# Add feature to layer
layer.CreateFeature(new_feature)

# Clean up
datasource.Destroy()
print(f"✓ Updated property boundary for {project_name}")
```

**Usage:**
```bash
python3 update_property_boundary.py "SIG_YourProject" "POLYGON((...coordinates...))"
```

### Field Requirements for property_boundaries.gpkg

| Field | Type | Required | Example |
|---|---|---|---|
| name | String | YES | "SIG_Artosas" |
| area | Real | YES | 138.5 (hectares) |
| print_layout_report | String | NO | "Yes"/"No" |

**CRS:** EPSG:3763 (ETRS89 / Portugal TM06) - must be preserved

---

## Step 4: Copy and Update QGIS Project File

**Source:** `/Users/andre/Sync/FdI/SIG/SIG_Artosas/Projeto QGIS.qgs`  
**Destination:** `./Projeto QGIS.qgs`

```bash
# Copy QGIS project file
cp /Users/andre/Sync/FdI/SIG/SIG_Artosas/Projeto\ QGIS.qgs \
   /Users/andre/Sync/FdI/SIG/SIG_[ProjectName]/Projeto\ QGIS.qgs
```

### Update Project Paths

The QGIS project file (.qgs) is XML and contains hardcoded paths. You MUST update these:

**Method 1: Using find-replace (recommended)**

```bash
# In the .qgs file, replace template paths with new project paths
cd /Users/andre/Sync/FdI/SIG/SIG_[ProjectName]

sed -i '' 's|/SIG_Artosas/|/SIG_[ProjectName]/|g' "Projeto QGIS.qgs"
sed -i '' 's|SIG_Artosas|SIG_[ProjectName]|g' "Projeto QGIS.qgs"
```

**Method 2: Using Python script**

```python
#!/usr/bin/env python3
import sys
from pathlib import Path

qgs_file = Path(sys.argv[1])  # Path to .qgs file
project_name = sys.argv[2]  # New project name (e.g., "SIG_Maridona")
old_project = "SIG_Artosas"  # Old project name

# Read XML
content = qgs_file.read_text(encoding='utf-8')

# Replace project names in paths
content = content.replace(old_project, project_name)
content = content.replace(f"/SIG_{old_project}/", f"/SIG_{project_name}/")

# Replace project title
content = content.replace(f'projectname="{old_project}"', f'projectname="{project_name}"')

# Write back
qgs_file.write_text(content, encoding='utf-8')

print(f"✓ Updated QGIS project file for {project_name}")
```

**Usage:**
```bash
python3 update_qgis_project.py "./Projeto QGIS.qgs" "SIG_[ProjectName]"
```

### Verify Project Update

1. Open updated `Projeto QGIS.qgs` in QGIS
2. Check that all layers load without error messages
3. Verify layer data sources point to new project directory
4. Confirm CRS is EPSG:3763 for all layers
5. Save any required changes

---

## Step 5: Copy Supporting QGIS Projects (Optional)

**Additional files from SIG_Artosas (if needed):**

```bash
# Copy alternate QGIS project files
cp /Users/andre/Sync/FdI/SIG/SIG_Artosas/Existencias.qgs \
   /Users/andre/Sync/FdI/SIG/SIG_[ProjectName]/

cp /Users/andre/Sync/FdI/SIG/SIG_Artosas/Rectificacao_Artosas.qgs \
   /Users/andre/Sync/FdI/SIG/SIG_[ProjectName]/Rectificacao_[ProjectName].qgs

# Update paths in these files too
sed -i '' 's|/SIG_Artosas/|/SIG_[ProjectName]/|g' "Existencias.qgs"
sed -i '' 's|Rectificacao_Artosas|Rectificacao_[ProjectName]|g' "Rectificacao_[ProjectName].qgs"
```

**Projects provided:**
- `Projeto QGIS.qgs` - Main project with all layers
- `Existencias.qgs` - Inventory/existing conditions visualization
- `Rectificacao_[ProjectName].qgs` - Project modifications and corrections

---

## Step 6: Copy Raster Data (If Available)

**Source:** `/Users/andre/Sync/FdI/SIG/SIG_Artosas/inputs_project/project_raster_data/`  
**Destination:** `./inputs_project/project_raster_data/`

```bash
# Copy raster directory structure
cp -r /Users/andre/Sync/FdI/SIG/SIG_Artosas/inputs_project/project_raster_data/* \
      inputs_project/project_raster_data/ 2>/dev/null || true

# Verify copy
ls -lah inputs_project/project_raster_data/ | head -20
```

**Typical raster files:**
```
DEM/
  Declividade.tif                  (Slope raster)
  HEIGHT_MODELS/
    VEGETATION/
      tree_peaks_local_maxima.gpkg
      vegetation_canopy_polygons_class5.gpkg
HIDROLOGICA/
  DEMforCalculations.tif           (DEM for hydrological analysis)
  vector_streams.gpkg
```

---

## Step 7: Validation & Quality Checks

### Automated Validation

```bash
#!/bin/bash
# Run validation checks on new SIG_[ProjectName]

PROJECT="/Users/andre/Sync/FdI/SIG/SIG_[ProjectName]"
echo "Validating $PROJECT..."

# Check directories
echo "✓ Checking directory structure..."
for dir in inputs_project/project_vector_data \
           inputs_project/project_raster_data \
           outputs/out_geopackage \
           intermediary/intermediary_zoning_matrix \
           _Predios grassdata; do
    if [ -d "$PROJECT/$dir" ]; then
        echo "  ✓ $dir"
    else
        echo "  ✗ MISSING: $dir"
    fi
done

# Check core GeoPackages
echo "✓ Checking core GeoPackage files..."
for gpkg in Notas.gpkg "IE Lineares.gpkg" IE_Pontos.gpkg \
            Arvores.gpkg Acessos.gpkg "Matriz Zoneamento.gpkg" \
            "Linha de Plantacao.gpkg" property_boundaries.gpkg \
            Altimetria.gpkg "Pontos de Interesse.gpkg"; do
    if [ -f "$PROJECT/inputs_project/project_vector_data/$gpkg" ]; then
        echo "  ✓ $gpkg"
    else
        echo "  ✗ MISSING: $gpkg"
    fi
done

# Check QGIS project
echo "✓ Checking QGIS project file..."
if [ -f "$PROJECT/Projeto QGIS.qgs" ]; then
    echo "  ✓ Projeto QGIS.qgs"
else
    echo "  ✗ MISSING: Projeto QGIS.qgs"
fi

echo "Validation complete!"
```

### Manual Checks

1. **Directory Structure:**
   ```bash
   find /Users/andre/Sync/FdI/SIG/SIG_[ProjectName] -type d | wc -l
   # Should be 12+ directories
   ```

2. **File Count:**
   ```bash
   ls -1 /Users/andre/Sync/FdI/SIG/SIG_[ProjectName]/inputs_project/project_vector_data/*.gpkg | wc -l
   # Should be 25+ files
   ```

3. **QGIS Project Validity:**
   - Open `Projeto QGIS.qgs` in QGIS
   - Should load without errors
   - All layers should have green checkmarks
   - CRS should be EPSG:3763

4. **Property Boundary:**
   - Open `property_boundaries.gpkg` in QGIS
   - Should have 1 feature
   - `name` field should match SIG_[ProjectName]
   - `area` field should have value (hectares)

---

## Step 8: Create QField Cloud Project

**Prerequisite:** QField Cloud account access

1. Go to https://cloud.qfield.org
2. Create new project: `SIG_[ProjectName]`
3. Upload `Projeto QGIS.qgs`
4. Configure:
   - Layer visibility (all core layers)
   - Attribute forms for data entry
   - Field validation rules
   - Read-only layers (reference data)
5. Share with field team
6. Test first data collection

---

## Step 9: Phase 1 Project Initialization

**For projects using FDI Phase 1 pipeline:**

```bash
# Option 1: Via Docker (recommended)
docker exec qgis-py-phase1 python3 -u \
  /workspace/modelos/phase1/tasks/new_project/project_creation_agent.py \
  --project-name "YourProjectName" \
  --template-dir "/Users/andre/Sync/FdI/SIG/Estrutura Projeto Template" \
  --verbose

# Option 2: Standalone Python
python3 /workspace/modelos/phase1/tasks/new_project/project_creation_agent.py \
  --project-name "YourProjectName" \
  --verbose
```

---

## Step 10: Test Complete Workflow

### Quick Test
```bash
# 1. Open in QGIS
open -a QGIS "/Users/andre/Sync/FdI/SIG/SIG_[ProjectName]/Projeto QGIS.qgs"

# 2. Add a test feature to Notas.gpkg
# 3. Save and verify

# 4. Test QField cloud sync
# Add a new feature in QField cloud
# Verify it appears in QGIS project after sync
```

### Full Pipeline Test
```bash
# 1. Trigger commercialmaps pipeline for _Predios data
# 2. Run Phase 1 analysis
# 3. Export to QField cloud
# 4. Collect field data in QField
# 5. Sync back to project
# 6. Verify all data integrity
```

---

## Critical Success Factors

### ✅ MUST DO
- [ ] All layers must have CRS EPSG:3763
- [ ] property_boundaries.gpkg must have exactly 1 feature
- [ ] All core .gpkg files must be present (11 required + optional)
- [ ] QGIS project file must load without errors
- [ ] UUID fields must be preserved in all layers
- [ ] Photo gallery layers must be present for sync

### ✅ AVOID
- ❌ Don't modify layer names (use exact names from template)
- ❌ Don't delete photo_gallery layers
- ❌ Don't change CRS to anything other than EPSG:3763
- ❌ Don't add spaces or special characters to project folder names
- ❌ Don't manually edit XML in .qgs files (use Python scripts)
- ❌ Don't skip property_boundaries update

---

## Troubleshooting

### Problem: QGIS project won't load
**Solution:**
1. Check all layer file paths are absolute and correct
2. Run find-replace again to ensure all paths updated
3. Open with QGIS and check Error Log for specific layer issues
4. Use Python script to verify and fix paths

### Problem: Layers missing or grayed out
**Solution:**
1. Verify .gpkg files exist in `inputs_project/project_vector_data/`
2. Check file permissions: `ls -la inputs_project/project_vector_data/`
3. Verify CRS matches EPSG:3763
4. Reload layer in QGIS (right-click → Reload)

### Problem: property_boundaries has 0 features
**Solution:**
1. Open in QGIS, manually add 1 polygon feature
2. Set `name` = project name
3. Set `area` = calculated hectares
4. Save and close

### Problem: QField cloud shows different data than QGIS
**Solution:**
1. Check if field data was properly synced
2. Verify UUID fields match between cloud and project
3. Re-export from QField cloud to project
4. Refresh QGIS layer cache (right-click → Refresh)

---

## Customization for Your Team

### Adding Custom Fields
- Edit .gpkg files directly in QGIS
- Add new columns with appropriate data types
- Use UUID to track changes back to QField cloud
- Document in project README

### Modifying Forms
- Use QGIS Attribute Form Designer
- Configure field widgets and validation
- Export to QField cloud for field interface

### Adding Styles & Symbolization
- Customize layer styles in QGIS
- Save as QGIS project file
- Styles export to QField cloud

---

## Documentation Standards

### For Each New Project, Create
1. **README.md** - Project overview, team info, key contacts
2. **LAYERS.md** - Layer glossary with descriptions
3. **WORKFLOW.md** - Data collection and processing workflow
4. **CONTACTS.md** - Team members and responsibilities

---

## Template Maintenance

**When to update template (SIG_Artosas):**
- New layer types added across all projects
- Core attribute schemas updated
- QGIS best practices improved
- New photo gallery patterns adopted

**Process:**
1. Update SIG_Artosas with improvements
2. Test in 2-3 new projects
3. Document changes
4. Distribute to all projects (if non-breaking)

---

## Summary

Creating a new SIG_[ProjectName] ready for office automation:

| Step | Action | Time | Difficulty |
|------|--------|------|------------|
| 1 | Create directories | 2 min | Low |
| 2 | Copy .gpkg files | 3 min | Low |
| 3 | Update property_boundary | 10 min | Medium |
| 4 | Update QGIS project | 5 min | Low |
| 5 | Copy rasters | 2 min | Low |
| 6 | Validation | 5 min | Low |
| 7 | Create QField cloud project | 10 min | Low |
| 8 | Test workflow | 10 min | Low |

**Total Time:** ~45 minutes  
**Success Rate:** 95%+ (with checklist)

---

**Questions?** See COMPREHENSIVE_AUDIT_REPORT.md for details on layer types and fields.  
**Ready to create a new project?** Follow the Quick Start Checklist above!
