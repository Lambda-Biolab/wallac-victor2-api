"""Add 610nm absorbance protocols as factory presets in Mlr3.mdb.

The 610nm filter is physically in slot 7 of the CW-lamp filter wheel A.
This script adds the filter, photometry labels, and assay protocols
directly into the Jet database so they survive VM restarts.

IMPORTANT: On Windows 7 with UAC, writes to C:\\Program Files\\... get
redirected to the VirtualStore path. The agent reads from the
VirtualStore path, so we must write there too.
"""
import comtypes.client
import datetime
import os

# Try VirtualStore path first (where the agent reads from), then fall
# back to the real Program Files path.
MDB_PATHS = [
    r"C:\Users\lambda\AppData\Local\VirtualStore\Program Files\Wallac\Wallac1420\Data\Mlr3.mdb",
    r"C:\Program Files\Wallac\Wallac1420\Data\Mlr3.mdb",
]
MDB_SRC = next((p for p in MDB_PATHS if os.path.exists(p)), MDB_PATHS[0])
print(f"Using MDB: {MDB_SRC}")

eng = comtypes.client.CreateObject("DAO.DBEngine.36")
db = eng.OpenDatabase(MDB_SRC, False, False)

now = datetime.datetime.now()
now_str = now.strftime("#%m/%d/%Y %H:%M:%S#")

# --- 0. Clean up ---
db.Execute("DELETE FROM Photometry WHERE LabelName LIKE 'Absorbance @ 610%'")
db.Execute("DELETE FROM AssayProtocol WHERE ProtName LIKE 'Absorbance @ 610%'")
print("Cleaned up")

filter_id = 14  # P610 already created

# --- 2. Create Photometry labels ---
rs = db.OpenRecordset("SELECT MAX(LabelID) AS MaxID FROM Photometry")
max_label = rs.Fields.Item("MaxID").Value or 2000000
rs.Close()
label_id_1s = max_label + 1
label_id_01s = max_label + 2

db.Execute(
    f"INSERT INTO Photometry (LabelID, LabelName, CWLampFilterID, "
    f"CWLampFilterID2, MeasTime, PolarizerAperture, UVAbsorbance, "
    f"FlashLampFilter, FactoryPreset, LastEditedWho, LastEditedTime, "
    f"ReadFromInstrument) "
    f"VALUES ({label_id_1s}, 'Absorbance @ 610 (1.0s)', {filter_id}, 0, 1.0, 0, False, 0, True, 'LabCopilot', {now_str}, False)"
)
print(f"Label 1.0s: id={label_id_1s}")

db.Execute(
    f"INSERT INTO Photometry (LabelID, LabelName, CWLampFilterID, "
    f"CWLampFilterID2, MeasTime, PolarizerAperture, UVAbsorbance, "
    f"FlashLampFilter, FactoryPreset, LastEditedWho, LastEditedTime, "
    f"ReadFromInstrument) "
    f"VALUES ({label_id_01s}, 'Absorbance @ 610 (0.1s)', {filter_id}, 0, 0.1, 0, False, 0, True, 'LabCopilot', {now_str}, False)"
)
print(f"Label 0.1s: id={label_id_01s}")

# --- 3. Create protocols via SQL INSERT (skip PlateMap — it will be NULL) ---
rs = db.OpenRecordset("SELECT MAX(AssayProtID) AS MaxID FROM AssayProtocol")
max_prot = rs.Fields.Item("MaxID").Value or 1000000
rs.Close()
prot_id_1s = max_prot + 1
prot_id_01s = max_prot + 2

# Copy the 490nm protocol's PlateMap using SQL
# INSERT INTO ... SELECT with modifications
db.Execute(
    f"INSERT INTO AssayProtocol (AssayProtID, ProtVersion, ProtName, ProtNumber, "
    f"ProtGroup, MeasSequence, MeasHeight, PlateTypeID, RepCount, RepDelta, "
    f"PlateMap, MeasurementMode, PlateHeating, Temperature, CreatedTime, "
    f"CreatedWho, LastEditedTime, LastEditedWho, RunCount, PrnOutput, "
    f"PrnOutputFormat, PrnOutputWhen, FileOutput, FileOutputWhen, "
    f"FileOutputType, FileOutputOptions, FactoryPreset) "
    f"SELECT {prot_id_1s}, 1, 'Absorbance @ 610 (1.0s)', ProtNumber, "
    f"103, 'L:{label_id_1s};', MeasHeight, PlateTypeID, RepCount, RepDelta, "
    f"PlateMap, MeasurementMode, PlateHeating, Temperature, "
    f"{now_str}, 'LabCopilot', {now_str}, 'LabCopilot', 0, False, "
    f"31, 0, False, 0, 0, 31, True "
    f"FROM AssayProtocol WHERE AssayProtID=1000005"
)
print(f"Protocol 1.0s: id={prot_id_1s}")

db.Execute(
    f"INSERT INTO AssayProtocol (AssayProtID, ProtVersion, ProtName, ProtNumber, "
    f"ProtGroup, MeasSequence, MeasHeight, PlateTypeID, RepCount, RepDelta, "
    f"PlateMap, MeasurementMode, PlateHeating, Temperature, CreatedTime, "
    f"CreatedWho, LastEditedTime, LastEditedWho, RunCount, PrnOutput, "
    f"PrnOutputFormat, PrnOutputWhen, FileOutput, FileOutputWhen, "
    f"FileOutputType, FileOutputOptions, FactoryPreset) "
    f"SELECT {prot_id_01s}, 1, 'Absorbance @ 610 (0.1s)', ProtNumber, "
    f"103, 'L:{label_id_01s};', MeasHeight, PlateTypeID, RepCount, RepDelta, "
    f"PlateMap, MeasurementMode, PlateHeating, Temperature, "
    f"{now_str}, 'LabCopilot', {now_str}, 'LabCopilot', 0, False, "
    f"31, 0, False, 0, 0, 31, True "
    f"FROM AssayProtocol WHERE AssayProtID=1000005"
)
print(f"Protocol 0.1s: id={prot_id_01s}")

db.Close()

# --- 4. Verify ---
db2 = eng.OpenDatabase(MDB_SRC, False, True)
rs = db2.OpenRecordset("SELECT AssayProtID, ProtName, MeasSequence, FactoryPreset FROM AssayProtocol WHERE ProtName LIKE '%610%'")
print("\n=== Verification ===")
while not rs.EOF:
    print(f"  id={rs.Fields.Item('AssayProtID').Value} name={rs.Fields.Item('ProtName').Value!r} "
          f"seq={rs.Fields.Item('MeasSequence').Value!r} factory={rs.Fields.Item('FactoryPreset').Value}")
    rs.MoveNext()
rs.Close()
db2.Close()
print("\nDone.")
