from __future__ import annotations
import math
import pandas as pd


class EmployerIndex:
    def __init__(self, conn):
        self.conn = conn
        self.df = pd.DataFrame()
        self.refresh()

    def refresh(self):
        f = pd.read_sql_query("SELECT * FROM filings", self.conn)
        if f.empty:
            self.df = pd.DataFrame(columns=["norm", "name", "naics", "state", "city", "years", "app_total", "den_total",
                                            "new_app_latest", "cont_app_latest", "other_app_latest", "app_latest",
                                            "den_latest", "volume", "sponsor_score", "approval_rate"])
            self.latest_fy = None
            self.f = f
            return
        # the same FY can arrive from two files; keep the largest source per (norm, fy, city, zip) to avoid double counting
        f["app"] = f.new_app + f.cont_app + f.other_app
        f["den"] = f.new_den + f.cont_den + f.other_den
        f = (f.sort_values("app", ascending=False)
               .drop_duplicates(["norm", "fy", "city", "zip", "state", "naics"]))
        self.f = f
        latest = int(f.fy.max())
        g = f.groupby("norm")

        def mode(col):
            t = (f.dropna(subset=[col]).groupby(["norm", col]).size().reset_index(name="n")
                   .sort_values(["norm", "n"], ascending=[True, False]).drop_duplicates("norm").set_index("norm")[col])
            return t

        df = pd.DataFrame({
            "name": mode("name"),
            "naics": mode("naics"),
            "state": mode("state"),
            "city": mode("city"),
            "years": g["fy"].nunique(),
            "app_total": g["app"].sum(),
            "den_total": g["den"].sum(),
        })
        cur = f[f.fy == latest].groupby("norm")[["new_app", "cont_app", "other_app", "app", "den"]].sum()
        cur = cur.add_suffix("_latest")
        df = df.join(cur)
        df[cur.columns] = df[cur.columns].fillna(0)
        # recency-weighted sponsorship volume, new hires count double
        w = f.assign(w=f.fy.map(lambda y: 0.7 ** (latest - y) if y else 0.3) * (2 * f.new_app + f.cont_app + f.other_app))
        df["volume"] = w.groupby("norm")["w"].sum()
        rate = df.app_total / (df.app_total + df.den_total).clip(lower=1)
        mx = math.log1p(df.volume.max() or 1)
        df["sponsor_score"] = (df.volume.map(math.log1p) / mx * (0.5 + 0.5 * rate)).clip(0, 1)
        df["approval_rate"] = rate
        self.latest_fy = latest
        self.df = df.reset_index()

    def search(self, q="", state="", industry="", min_approvals=0, limit=100, has_jobs_only=False, with_jobs=None):
        d = self.df
        if q:
            d = d[d.name.str.contains(q, case=False, na=False, regex=False)]
        if state:
            d = d[d.state == state.upper()]
        if industry:
            d = d[d.naics.fillna("").str.startswith(industry)]
        if min_approvals:
            d = d[d.app_total >= min_approvals]
        if has_jobs_only and with_jobs is not None:
            d = d[d.norm.isin(with_jobs)]
        return d.sort_values("sponsor_score", ascending=False).head(limit)

    def history(self, norm: str):
        h = self.f[self.f.norm == norm].groupby("fy")[["new_app", "new_den", "cont_app", "cont_den", "other_app", "other_den"]].sum()
        return h.reset_index().to_dict("records")

    def facets(self):
        d = self.df
        return {
            "states": sorted(d.state.dropna().unique().tolist()),
            "industries": sorted({str(x) for x in d.naics.dropna().unique()}),
        }
