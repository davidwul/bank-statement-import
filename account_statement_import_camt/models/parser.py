"""Class to parse camt files."""
# Copyright 2013-2016 Therp BV <https://therp.nl>
# Copyright 2017 Open Net Sàrl
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl.html).

import re
from lxml import etree
from odoo import _, models


class CamtParser(models.AbstractModel):
    _name = "account.statement.import.camt.parser"
    _description = "Account Bank Statement Import CAMT parser"

    def parse_amount(self, ns, node):
        """Parse Amount only from the direct child of the provided node."""
        if node is None:
            return 0.0
        sign = 1
        sign_node = node.xpath("ns:CdtDbtInd", namespaces={"ns": ns})
        if not sign_node:
            sign_node = node.xpath("../../ns:CdtDbtInd", namespaces={"ns": ns})
        if sign_node and sign_node[0].text == "DBIT":
            sign = -1
        amount_node = node.xpath("ns:Amt", namespaces={"ns": ns})
        if amount_node:
            return sign * float(amount_node[0].text)
        return 0.0

    def parse_amount_details_currency(self, ns, node, transaction):
        """Discover foreign currency and handle exchange rate calculations."""
        add_currency = False
        ntry_dtls_currency = None
        currency_amount = 0.0
        trgt_ccy = node.xpath(".//ns:CcyXchg/ns:TrgtCcy", namespaces={"ns": ns})
        if trgt_ccy and transaction.get("currency") != trgt_ccy[0].text:
            rate = node.xpath(".//ns:CcyXchg/ns:XchgRate", namespaces={"ns": ns})
            amt_main = node.xpath("ns:Amt", namespaces={"ns": ns})
            if rate and amt_main:
                ntry_dtls_currency = trgt_ccy[0].text
                currency_amount = float(amt_main[0].text) * float(rate[0].text)
                add_currency = True
        if not add_currency:
            ccy_nodes = node.xpath(".//ns:AmtDtls//@Ccy", namespaces={"ns": ns})
            for ccy in ccy_nodes:
                if ccy != transaction.get("currency"):
                    val_node = node.xpath(
                        f".//ns:AmtDtls//*[@Ccy='{ccy}']",
                        namespaces={"ns": ns}
                    )
                    if val_node:
                        ntry_dtls_currency = ccy
                        currency_amount = float(val_node[0].text)
                        add_currency = True
                        break
        if add_currency and ntry_dtls_currency:
            other_currency = self.env["res.currency"].search(
                [("name", "=", ntry_dtls_currency)], limit=1
            )
            if other_currency:
                sign = 1 if transaction.get("amount", 0) >= 0 else -1
                transaction["amount_currency"] = currency_amount * sign
                transaction["foreign_currency_id"] = other_currency.id
        return add_currency

    def parse_entry(self, ns, node):
        """Parse Ntry node and yield transactions (handles single/batch)."""
        transaction_base = {
            "payment_ref": "/",
            "amount": 0.0,
            "narration": {},
            "transaction_type": {},
        }
        self.add_value_from_node(
            ns, node, "./ns:BookgDt/ns:Dt | ./ns:BookgDt/ns:DtTm",
            transaction_base, "date"
        )
        self.add_value_from_node(
            ns, node, ["./ns:Amt/@Ccy", "./ns:AmtDtls/ns:TxAmt/ns:Amt/@Ccy"],
            transaction_base, "currency"
        )
        entry_amount = self.parse_amount(ns, node)
        self.add_value_from_node(
            ns, node,
            ["./ns:NtryDtls/ns:RmtInf/ns:Strd/ns:CdtrRefInf/ns:Ref",
             "./ns:NtryDtls/ns:Btch/ns:PmtInfId",
             "./ns:NtryDtls/ns:TxDtls/ns:Refs/ns:AcctSvcrRef",
             "./ns:AcctSvcrRef"], transaction_base, "ref"
        )
        self.add_value_from_node(
            ns, node, ["./ns:AddtlNtryInf"], transaction_base, "payment_ref"
        )
        self.add_value_from_node(
            ns, node, "./ns:AddtlNtryInf", transaction_base["narration"],
            "%s (AddtlNtryInf)" % _("Additional Entry Information")
        )
        self.add_value_from_node(
            ns, node, "./ns:RvslInd", transaction_base["narration"],
            "%s (RvslInd)" % _("Reversal Indicator")
        )
        for code_path, key in [("./ns:BkTxCd/ns:Domn/ns:Cd", "Code"),
                               ("./ns:BkTxCd/ns:Domn/ns:Fmly/ns:Cd", "FmlyCd"),
                               ("./ns:BkTxCd/ns:Domn/ns:Fmly/ns:SubFmlyCd",
                                "SubFmlyCd")]:
            self.add_value_from_node(
                ns, node, code_path, transaction_base["transaction_type"], key
            )
        transaction_base["transaction_type"] = "-".join(
            transaction_base["transaction_type"].values()
        ) or ""

        details_nodes = node.xpath(
            "./ns:NtryDtls/ns:TxDtls", namespaces={"ns": ns}
        )
        chrg_inc = node.xpath(
            "./ns:Chrgs/ns:Rcrd/ns:ChrgInclInd", namespaces={"ns": ns}
        )
        if chrg_inc and chrg_inc[0].text == "true":
            details_nodes += node.xpath(
                "./ns:Chrgs/ns:Rcrd", namespaces={"ns": ns}
            )

        if not details_nodes:
            transaction = transaction_base.copy()
            transaction["amount"] = entry_amount
            self.parse_amount_details_currency(ns, node, transaction)
            transaction.pop("currency", None)
            self.generate_narration(transaction)
            yield transaction
            return

        for det_node in details_nodes:
            transaction = transaction_base.copy()
            transaction["narration"] = transaction_base["narration"].copy()
            detail_amount = self.parse_amount(ns, det_node)
            transaction["amount"] = (
                detail_amount if detail_amount != 0.0 else entry_amount
            )
            self.parse_transaction_details(ns, det_node, transaction)
            if not self.parse_amount_details_currency(ns, det_node,
                                                      transaction):
                self.parse_amount_details_currency(ns, node, transaction)
            transaction.pop("currency", None)
            self.generate_narration(transaction)
            yield transaction

    def add_value_from_node(self, ns, node, xpath_str, obj, attr, join_str=None):
        if not isinstance(xpath_str, (list, tuple)):
            xpath_str = [xpath_str]
        for search_str in xpath_str:
            found_node = node.xpath(search_str, namespaces={"ns": ns})
            if found_node:
                if isinstance(found_node[0], str):
                    attr_value = found_node[0]
                elif join_str is None:
                    attr_value = found_node[0].text
                else:
                    attr_value = join_str.join(
                        [x.text for x in found_node if x.text]
                    )
                obj[attr] = attr_value
                break

    def parse_transaction_details(self, ns, node, transaction):
        self.add_value_from_node(
            ns, node, ["./ns:RmtInf/ns:Ustrd|./ns:RtrInf/ns:AddtlInf",
                       "./ns:AddtlNtryInf", "./ns:Refs/ns:InstrId"],
            transaction, "payment_ref", join_str="\n"
        )
        # capture ALL multi-line unstructured info
        self.add_value_from_node(
            ns, node, ["./ns:RmtInf/ns:Ustrd"],
            transaction["narration"],
            _("Details"),
            join_str=" / "
        )
        self.add_value_from_node(
            ns, node, ["./ns:RmtInf/ns:Strd/ns:CdtrRefInf/ns:Ref",
                       "./ns:Refs/ns:EndToEndId", "./ns:Ntry/ns:AcctSvcrRef"],
            transaction, "ref"
        )
        ultmtdbtr = node.xpath(
            "./ns:RltdPties/ns:UltmtDbtr", namespaces={"ns": ns}
        )
        party_type = "UltmtDbtr" if ultmtdbtr else "Dbtr"
        party_type_node = node.xpath(
            "../../ns:CdtDbtInd", namespaces={"ns": ns}
        )
        if party_type_node and party_type_node[0].text != "CRDT":
            party_type = "Cdtr"
        party_node = node.xpath(
            "./ns:RltdPties/ns:%s" % party_type, namespaces={"ns": ns}
        )
        if party_node:
            name_node = node.xpath(
                "./ns:RltdPties/ns:{pt}/ns:Nm | "
                "./ns:RltdPties/ns:{pt}/ns:Pty/ns:Nm".format(pt=party_type),
                namespaces={"ns": ns}
            )
            if name_node:
                transaction["partner_name"] = name_node[0].text
            self.add_value_from_node(
                ns, party_node[0],
                "./ns:PstlAdr/ns:StrtNm|./ns:PstlAdr/ns:Ctry|"
                "./ns:PstlAdr/ns:AdrLine", transaction["narration"],
                "%s (PstlAdr)" % _("Postal Address"), join_str=" | "
            )
        account_node = node.xpath(
            "./ns:RltdPties/ns:%sAcct/ns:Id" % party_type, namespaces={"ns": ns}
        )
        if account_node:
            iban_node = account_node[0].xpath(
                "./ns:IBAN", namespaces={"ns": ns}
            )
            if iban_node:
                transaction["account_number"] = iban_node[0].text
            else:
                self.add_value_from_node(
                    ns, account_node[0], "./ns:Othr/ns:Id",
                    transaction, "account_number"
                )

    def generate_narration(self, transaction):
        """Build the final narration string including fees and communications."""
        # We start with the base information
        narr_parts = {
            _("Partner Name"): transaction.get("partner_name", ""),
            _("Reference"): transaction.get("ref", ""),
            _("Communication"): transaction.get("payment_ref", ""),
        }
        res = []
        for title, value in narr_parts.items():
            if value:
                res.append("%s: %s" % (title, value))
        for title, value in transaction.get("narration", {}).items():
            if value and value not in narr_parts.values():
                res.append("%s: %s" % (title, value))

        transaction["narration"] = "\n".join(res)

    def get_balance_amounts(self, ns, node):
        start_bal = end_bal = 0.0
        for code in ["OPBD", "PRCD", "CLBD", "ITBD"]:
            expr = (
                './ns:Bal/ns:Tp/ns:CdOrPrtry/ns:Cd[text()="%s"]/../../..' % code
            )
            bal_node = node.xpath(expr, namespaces={"ns": ns})
            if bal_node:
                if code in ["OPBD", "PRCD"]:
                    start_bal = self.parse_amount(ns, bal_node[0])
                elif code == "CLBD":
                    end_bal = self.parse_amount(ns, bal_node[0])
                else:
                    if not start_bal:
                        start_bal = self.parse_amount(ns, bal_node[0])
                    end_bal = self.parse_amount(ns, bal_node[-1])
        return start_bal, end_bal

    def parse_statement(self, ns, node):
        result = {}
        self.add_value_from_node(
            ns, node, ["./ns:Acct/ns:Id/ns:IBAN", "./ns:Acct/ns:Id/ns:Othr/ns:Id"],
            result, "account_number"
        )
        self.add_value_from_node(ns, node, "./ns:Id", result, "name")

        # Chemins étendus pour trouver la devise (Acct -> Bal -> Ntry)
        self.add_value_from_node(
            ns, node, [
                "./ns:Acct/ns:Ccy",
                "./ns:Bal/ns:Amt/@Ccy",
                "./ns:Ntry/ns:Amt/@Ccy"
            ],
            result,
            "currency"
        )

        result["balance_start"], result["balance_end_real"] = (
            self.get_balance_amounts(ns, node)
        )
        transactions = []
        for entry_node in node.xpath("./ns:Ntry", namespaces={"ns": ns}):
            transactions.extend(list(self.parse_entry(ns, entry_node)))
        result["transactions"] = transactions
        result["date"] = None
        if transactions:
            result["date"] = sorted(
                transactions, key=lambda x: x["date"], reverse=True
            )[0]["date"]
        return result

    def check_version(self, ns, root):
        re_camt = re.compile(
            r"(^urn:iso:std:iso:20022:tech:xsd:camt\.|^ISO:camt\.)"
        )
        if not re_camt.search(ns):
            raise ValueError("no camt: " + ns)
        re_camt_version = re.compile(
            r"(^urn:iso:std:iso:20022:tech:xsd:camt\.05[2-4]\.|^ISO:camt\.05[2-4]\.)"
        )
        if not re_camt_version.search(ns):
            raise ValueError("no camt 052, 053 or 054: " + ns)
        root_0_0 = root[0][0].tag[len(ns) + 2:]
        if root_0_0 != "GrpHdr":
            raise ValueError("expected GrpHdr, got: " + root_0_0)

    def parse(self, data):
        try:
            root = etree.fromstring(data, parser=etree.XMLParser(recover=True))
        except etree.XMLSyntaxError:
            try:
                root = etree.fromstring(
                    data.decode("iso-8859-15").encode("utf-8")
                )
            except etree.XMLSyntaxError:
                root = None
        if root is None:
            raise ValueError("Not a valid xml file.")
        ns = root.tag[1:root.tag.index("}")]
        self.check_version(ns, root)
        statements = []
        currency = account_number = None
        for node in root[0][1:]:
            statement = self.parse_statement(ns, node)
            if statement["transactions"]:
                # Récupération sécurisée de la devise du relevé
                st_currency = statement.pop("currency", None)
                if st_currency:
                    currency = st_currency
                st_account = statement.pop("account_number", None)
                if st_account:
                    account_number = st_account
                statements.append(statement)
        return currency, account_number, statements
