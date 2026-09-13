"""
Fictitious company, vendor and catalog data for the invoice pipeline demo.

Everything here is invented. No real company, person, address, phone number,
tax ID or bank detail appears in this file. Addresses use fictional street
names in real cities; phone numbers use the 555 exchange reserved for fiction.
"""

# ---------------------------------------------------------------------------
# The buyer — the demo company whose AP pipeline we are building
# ---------------------------------------------------------------------------

BUYER = {
    "name": "Northgate Supply Co.",
    "addr1": "4180 Wexford Trace",
    "addr2": "Suite 220",
    "city": "Marietta",
    "state": "GA",
    "zip": "30062",
    "phone": "(770) 555-0142",
    "ap_email": "ap@northgatesupply.example",
}

# ---------------------------------------------------------------------------
# Vendors — the known-good master list.
# `category` drives which line items appear on their invoices.
# `tax_rate` is the sales tax applied to taxable subtotals.
# ---------------------------------------------------------------------------

VENDORS = [
    {
        "name": "Caldwell Paper & Packaging",
        "category": "packaging",
        "addr1": "2207 Fallbrook Road",
        "city": "Chattanooga", "state": "TN", "zip": "37402",
        "phone": "(423) 555-0111",
        "terms": "Net 30", "tax_rate": 0.0925, "has_freight": True,
        "account": "Packaging Supplies",
    },
    {
        "name": "Brightline Freight Systems",
        "category": "freight",
        "addr1": "915 Dunmore Industrial Pkwy",
        "city": "Atlanta", "state": "GA", "zip": "30336",
        "phone": "(404) 555-0165",
        "terms": "Net 15", "tax_rate": 0.0, "has_freight": False,
        "account": "Freight & Delivery",
    },
    {
        "name": "Verity Office Solutions",
        "category": "office",
        "addr1": "77 Ashmont Center Drive",
        "city": "Birmingham", "state": "AL", "zip": "35203",
        "phone": "(205) 555-0159",
        "terms": "Net 30", "tax_rate": 0.10, "has_freight": True,
        "account": "Office Supplies",
    },
    {
        "name": "Ironwood Components LLC",
        "category": "components",
        "addr1": "3390 Harlow Bend",
        "city": "Greenville", "state": "SC", "zip": "29601",
        "phone": "(864) 555-0186",
        "terms": "2/10 Net 30", "tax_rate": 0.06, "has_freight": True,
        "account": "Raw Materials",
    },
    {
        "name": "Summit Ridge Utilities",
        "category": "utilities",
        "addr1": "1 Cormorant Plaza",
        "city": "Marietta", "state": "GA", "zip": "30060",
        "phone": "(678) 555-0178",
        "terms": "Due on Receipt", "tax_rate": 0.0, "has_freight": False,
        "account": "Utilities",
    },
    {
        "name": "Praxis Software Group",
        "category": "software",
        "addr1": "600 Kestrel Way",
        "city": "Austin", "state": "TX", "zip": "78701",
        "phone": "(512) 555-0125",
        "terms": "Net 30", "tax_rate": 0.0825, "has_freight": False,
        "account": "Software Subscriptions",
    },
    {
        "name": "Halvorsen Machine Works",
        "category": "equipment",
        "addr1": "8842 Tanner Mill Road",
        "city": "Knoxville", "state": "TN", "zip": "37917",
        "phone": "(865) 555-0156",
        "terms": "Net 30", "tax_rate": 0.07, "has_freight": True,
        "account": "Equipment Repair & Maintenance",
    },
    {
        "name": "Coastal Label & Print",
        "category": "printing",
        "addr1": "412 Bayliss Street",
        "city": "Savannah", "state": "GA", "zip": "31401",
        "phone": "(912) 555-0123",
        "terms": "Net 30", "tax_rate": 0.07, "has_freight": True,
        "account": "Printing & Labels",
    },
    {
        "name": "Meridian Professional Services",
        "category": "professional",
        "addr1": "255 Lockhart Avenue, Floor 9",
        "city": "Charlotte", "state": "NC", "zip": "28202",
        "phone": "(704) 555-0145",
        "terms": "Net 15", "tax_rate": 0.0, "has_freight": False,
        "account": "Professional Fees",
    },
    {
        "name": "Tri-State Electric Supply",
        "category": "electrical",
        "addr1": "1720 Penwood Court",
        "city": "Nashville", "state": "TN", "zip": "37210",
        "phone": "(615) 555-0100",
        "terms": "Net 30", "tax_rate": 0.0925, "has_freight": True,
        "account": "Shop Supplies",
    },
    {
        "name": "Oakfield Logistics Partners",
        "category": "logistics",
        "addr1": "5501 Merrow Gate Boulevard",
        "city": "Jacksonville", "state": "FL", "zip": "32218",
        "phone": "(904) 555-0163",
        "terms": "Net 30", "tax_rate": 0.0, "has_freight": False,
        "account": "Warehousing & Fulfillment",
    },
    {
        "name": "Kessler Industrial Tools",
        "category": "tools",
        "addr1": "308 Ridgeline Spur",
        "city": "Huntsville", "state": "AL", "zip": "35801",
        "phone": "(256) 555-0125",
        "terms": "Net 30", "tax_rate": 0.09, "has_freight": True,
        "account": "Small Tools",
    },
]

# A vendor deliberately NOT in the master list, used for the UNKNOWN_VENDOR case.
ROGUE_VENDOR = {
    "name": "Pinehurst Facility Services",
    "category": "professional",
    "addr1": "44 Wilbraham Court",
    "city": "Macon", "state": "GA", "zip": "31201",
    "phone": "(478) 555-0124",
    "terms": "Net 30", "tax_rate": 0.0, "has_freight": False,
    "account": "Building Maintenance",
}

# ---------------------------------------------------------------------------
# Line item catalogs by vendor category.
# (description, unit, unit_price_low, unit_price_high, qty_low, qty_high)
# ---------------------------------------------------------------------------

CATALOG = {
    "packaging": [
        ("Corrugated carton, 12x9x6, ECT-32", "CS", 42.50, 68.00, 4, 40),
        ("Corrugated carton, 18x14x10, ECT-44", "CS", 74.00, 112.00, 2, 24),
        ("Void fill paper, 15in x 1800ft roll", "RL", 88.00, 134.00, 1, 12),
        ("Poly mailer, 10x13, 2.5 mil", "CS", 31.00, 49.50, 5, 30),
        ("Pressure-sensitive carton tape, 2in x 110yd", "CS", 54.00, 79.00, 2, 18),
        ("Stretch film, 18in x 1500ft, 80ga", "CS", 96.00, 142.00, 1, 10),
        ("Edge protector, 2x2x48, V-board", "BD", 1.85, 3.40, 50, 400),
        ("Kraft paper roll, 36in x 900ft", "RL", 62.00, 98.00, 1, 8),
    ],
    "freight": [
        ("LTL shipment, Atlanta GA to Charlotte NC", "EA", 285.00, 640.00, 1, 3),
        ("LTL shipment, Atlanta GA to Nashville TN", "EA", 310.00, 590.00, 1, 3),
        ("Residential delivery surcharge", "EA", 42.00, 88.00, 1, 6),
        ("Liftgate service", "EA", 55.00, 95.00, 1, 5),
        ("Fuel surcharge", "EA", 38.50, 121.00, 1, 4),
        ("Reconsignment fee", "EA", 65.00, 110.00, 1, 2),
        ("Detention, per hour after 2 hrs free", "HR", 75.00, 95.00, 1, 6),
    ],
    "office": [
        ("Multipurpose paper, 20lb, 8.5x11, 10-ream case", "CS", 46.00, 62.00, 2, 20),
        ("Toner cartridge, high yield, black", "EA", 118.00, 189.00, 1, 8),
        ("File folders, letter, 1/3 cut, box of 100", "BX", 22.50, 36.00, 2, 15),
        ("Ballpoint pen, medium, box of 60", "BX", 14.00, 24.00, 1, 12),
        ("Packing list envelopes, 7x5.5, case of 1000", "CS", 58.00, 86.00, 1, 6),
        ("Desk calendar, 12 month", "EA", 9.50, 18.00, 2, 20),
        ("Whiteboard markers, assorted, pack of 12", "PK", 11.00, 19.50, 1, 10),
    ],
    "components": [
        ("Hex cap screw, 3/8-16 x 1-1/2, zinc", "C", 18.40, 31.00, 5, 60),
        ("Flat washer, 3/8 SAE, zinc", "C", 4.20, 9.80, 10, 80),
        ("Compression spring, 0.75 OD x 2.00 FL", "EA", 2.15, 5.60, 25, 300),
        ("Aluminum extrusion, 6105-T5, 1.5in, 96in length", "EA", 38.00, 64.00, 4, 40),
        ("Ball bearing, 6203-2RS", "EA", 6.75, 14.20, 10, 120),
        ("Nylon spacer, 0.25 ID x 0.50 OD x 0.375", "C", 12.00, 26.50, 2, 30),
        ("Machined bracket, rev C, powder coat", "EA", 22.80, 47.00, 10, 150),
        ("Retaining ring, external, 1.00 shaft", "C", 9.40, 18.60, 3, 25),
    ],
    "utilities": [
        ("Electric service, 4180 Wexford Trace", "EA", 840.00, 2150.00, 1, 1),
        ("Natural gas service", "EA", 210.00, 780.00, 1, 1),
        ("Water and sewer", "EA", 145.00, 390.00, 1, 1),
        ("Commercial waste collection, 6yd", "EA", 185.00, 265.00, 1, 1),
    ],
    "software": [
        ("Fleet routing platform, annual, 25 seats", "EA", 2400.00, 4800.00, 1, 1),
        ("Warehouse scanning app, monthly, per device", "EA", 18.00, 34.00, 10, 60),
        ("Support plan, gold tier, annual", "EA", 1200.00, 2600.00, 1, 1),
        ("Additional API call bundle, 1M calls", "EA", 180.00, 340.00, 1, 8),
        ("Implementation services, remote", "HR", 145.00, 210.00, 2, 20),
    ],
    "equipment": [
        ("Forklift 90-day service, unit FL-2", "EA", 385.00, 620.00, 1, 2),
        ("Hydraulic hose assembly, replacement", "EA", 88.00, 164.00, 1, 8),
        ("Conveyor belt splice, on-site", "EA", 420.00, 780.00, 1, 3),
        ("Labor, field technician", "HR", 95.00, 145.00, 2, 16),
        ("Travel and mileage", "EA", 48.00, 132.00, 1, 4),
        ("Drive motor, 2HP, replacement", "EA", 640.00, 1180.00, 1, 2),
    ],
    "printing": [
        ("Thermal transfer label, 4x6, 1000/roll", "RL", 16.80, 27.50, 8, 96),
        ("Custom shipping label, 2-color, 5000 run", "M", 92.00, 148.00, 1, 10),
        ("Carton stamp, self-inking, custom plate", "EA", 68.00, 115.00, 1, 6),
        ("Product insert card, 4x6, 4/4, 10000 run", "M", 34.00, 58.00, 2, 20),
        ("Plate and setup charge", "EA", 75.00, 140.00, 1, 3),
        ("Proof, hard copy", "EA", 25.00, 45.00, 1, 4),
    ],
    "professional": [
        ("Monthly bookkeeping, close and reconciliation", "EA", 850.00, 1650.00, 1, 1),
        ("Consulting, process review", "HR", 165.00, 245.00, 2, 24),
        ("Annual report preparation", "EA", 1200.00, 2400.00, 1, 1),
        ("Advisory retainer", "EA", 600.00, 1500.00, 1, 1),
        ("Janitorial service, monthly contract", "EA", 680.00, 1240.00, 1, 1),
        ("Floor care, quarterly strip and wax", "EA", 420.00, 890.00, 1, 2),
    ],
    "electrical": [
        ("THHN wire, 12 AWG, 500ft spool", "EA", 82.00, 138.00, 1, 12),
        ("EMT conduit, 3/4in, 10ft stick", "EA", 8.40, 15.20, 10, 100),
        ("LED high bay fixture, 150W, 5000K", "EA", 118.00, 196.00, 2, 24),
        ("Motor starter, size 1, 480V", "EA", 165.00, 295.00, 1, 8),
        ("Junction box, 4-11/16 square, steel", "EA", 5.80, 11.40, 10, 80),
        ("Circuit breaker, 20A, 1-pole", "EA", 12.60, 24.80, 4, 40),
    ],
    "logistics": [
        ("Pallet storage, per pallet per month", "EA", 14.50, 26.00, 20, 260),
        ("Pick and pack, per order", "EA", 2.35, 4.90, 100, 1400),
        ("Receiving, per pallet", "EA", 9.00, 18.00, 5, 70),
        ("Kitting labor", "HR", 24.00, 38.00, 4, 40),
        ("Cycle count, quarterly", "EA", 340.00, 720.00, 1, 2),
    ],
    "tools": [
        ("Impact driver, 18V, bare tool", "EA", 128.00, 218.00, 1, 8),
        ("Carbide end mill, 1/2in, 4-flute", "EA", 28.50, 54.00, 2, 24),
        ("Digital caliper, 6in, IP54", "EA", 62.00, 118.00, 1, 10),
        ("Safety glasses, clear, box of 12", "BX", 34.00, 58.00, 1, 12),
        ("Cut-off wheel, 4.5in, box of 25", "BX", 41.00, 69.00, 1, 10),
        ("Torque wrench, 3/8 drive, calibrated", "EA", 145.00, 265.00, 1, 4),
    ],
}
