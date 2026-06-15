# How to Add an FDD to the Database

No typing of file paths, no terminal. There are **two ways** to add FDDs — pick
whichever you have.

---

## Option A — I already have the FDD PDF

1. **Double-click** the **`Add FDD to Database`** icon (on the Desktop).
   A window opens, on the **“Add a PDF file”** tab.

2. Click **“Choose FDD PDF…”** and pick the FDD file.

3. Click **“Add to Database”**.

## Option B — Just give it brand names (it finds the FDDs for you)

1. Open the tool and click the **“Fetch by brand name”** tab.

2. Type the franchise brand names — one per line (or comma-separated):

   ```
   KFC
   Taco Bell
   Wingstop
   ```

3. Click **“Fetch & Add”**.

The tool searches the **Wisconsin**, then **Minnesota**, then **California**
franchise registers, downloads each brand's FDD from the first one that has it,
and adds them all in one go.

---

Either way, a progress log shows what's happening. When it finishes you'll see
a pop-up like:

> ✅ Added Wingstop: 512 new locations.
> Database now holds 14,594 unique operators (2,310 in the ICP list).
> ✅ Saved to GitHub.

You can then close the window.

> **Note on “Fetch by brand name”:** it searches **Wisconsin → Minnesota →
> California** and uses the first one that has the FDD. Some brands aren't
> registered in any of them (for example, Subway) and will show as *“not found”*.
> For those, get the FDD PDF yourself and use **Option A**.
>
> If a brand comes back *“payment required”*, your Apify account is out of
> credits — tell your admin to top it up.

---

## Good to know

- **It can take a while.** A brand-new restaurant brand makes the tool compare
  every operator against the whole database. A few minutes is normal; a large
  new brand can take longer. Just leave the window open — it isn't frozen.

- **Adding the same FDD twice is safe.** It won't create duplicates or inflate
  the numbers.

- **Where's the data?** Click **“Open output folder”** in the window. The main
  file is **`icp_combined.csv`**.

---

## If it says it couldn't read any franchisees

This is rare. It means the tool couldn't automatically find the list of
franchisees inside the PDF.

1. Open the FDD and look at its **Table of Contents** for the exhibit titled
   something like *“List of Franchisees”* / *“List of Outlets”* — note its
   **page numbers** (for example, pages 230 to 309).
2. In the tool, tick **“Advanced options”**.
3. Type the range in **“Franchisee-list pages”** like `230-309`.
4. Click **“Add to Database”** again.

If you're still stuck, send the FDD to your admin.
