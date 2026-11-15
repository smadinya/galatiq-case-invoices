from agents.validation import check_address, resolve_vendor


def _check(repo, inv):
    return check_address(inv, resolve_vendor(inv, repo), repo)


def test_no_record_for_unknown_vendor(repo, make_invoice):
    inv = make_invoice(vendor="Fraudster LLC", address="1 Elm St")
    assert _check(repo, inv) == "no_record"


def test_no_record_for_missing_vendor(repo, make_invoice):
    assert _check(repo, make_invoice(vendor="", address="1 Elm St")) == "no_record"


def test_no_record_when_vendor_has_no_address_on_file(repo, make_invoice):
    # Gadgets Co. has no address in the master; no_record wins even if the invoice has one.
    assert _check(repo, make_invoice(vendor="Gadgets Co.", address="9 Any St")) == "no_record"
    assert _check(repo, make_invoice(vendor="Gadgets Co.", address=None)) == "no_record"


def test_not_on_invoice(repo, make_invoice):
    assert _check(repo, make_invoice(vendor="Widgets Inc.", address=None)) == "not_on_invoice"
    assert _check(repo, make_invoice(vendor="Widgets Inc.", address="   ")) == "not_on_invoice"


def test_verified_ignores_case_and_punctuation(repo, make_invoice):
    inv = make_invoice(vendor="Widgets Inc.", address="100 main st,  CHICAGO IL 60601")
    assert _check(repo, inv) == "verified"


def test_mismatch(repo, make_invoice):
    inv = make_invoice(vendor="Widgets Inc.", address="999 Other Rd, Chicago, IL 60601")
    assert _check(repo, inv) == "mismatch"


def test_renamed_vendor_uses_master_address(repo, make_invoice):
    inv = make_invoice(
        vendor="WidgetWorks", former_name="Widgets Inc.", address="100 Main St, Chicago, IL 60601"
    )
    assert resolve_vendor(inv, repo).kind == "renamed"
    assert _check(repo, inv) == "verified"
