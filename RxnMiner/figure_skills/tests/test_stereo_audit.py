from tools.chem_mcp import tool_verify_smiles


def test_verify_smiles_reports_tetrahedral_cip_and_indexed_svg():
    result = tool_verify_smiles("F[C@H](Cl)Br")
    assert result["valid"] is True
    assert result["stereocenters"] == [{
        "atom_index": 1,
        "element": "C",
        "cip": "R",
        "assigned": True,
        "chiral_tag": "CHI_TETRAHEDRAL_CCW",
    }]
    assert result["stereo_summary"]["tetrahedral_assigned"] == 1
    assert "atom-1" in result["indexed_svg"]


def test_verify_smiles_reports_e_and_z_double_bonds():
    e_result = tool_verify_smiles("F/C=C/F")
    z_result = tool_verify_smiles("F/C=C\\F")
    assert e_result["double_bond_stereo"][0]["stereo"] == "E"
    assert z_result["double_bond_stereo"][0]["stereo"] == "Z"
    assert e_result["double_bond_stereo"][0]["assigned"] is True
    assert z_result["double_bond_stereo"][0]["assigned"] is True


def test_verify_smiles_flags_unassigned_potential_stereo():
    tetra = tool_verify_smiles("FC(Cl)Br")
    alkene = tool_verify_smiles("FC=CF")
    assert tetra["stereocenters"][0]["cip"] == "?"
    assert tetra["stereocenters"][0]["assigned"] is False
    assert "potential tetrahedral stereocentre(s) are unassigned" in tetra["stereo_warnings"]
    assert alkene["double_bond_stereo"][0]["stereo"] == "UNSPECIFIED"
    assert alkene["double_bond_stereo"][0]["assigned"] is False
    assert "potential E/Z double bond(s) are unassigned" in alkene["stereo_warnings"]


def test_verify_smiles_invalid_input_is_still_safe():
    result = tool_verify_smiles("not-a-smiles")
    assert result["valid"] is False
    assert "reason" in result


def test_verify_smiles_expands_supported_concrete_esmiles_without_rewriting_input():
    raw = "*C<sep><a>0:Ph</a>"
    result = tool_verify_smiles(raw, require_concrete=True)
    assert result["valid"] is True
    assert result["input"] == raw
    assert result["rendering_scope"] == "full_expanded"
    assert result["esmiles_audit"]["concrete_valid"] is True
    assert result["esmiles_audit"]["known_abbreviations"] == ["Ph"]
    assert result["esmiles_audit"]["remaining_dummies"] == 0


def test_verify_smiles_fails_closed_for_open_or_unsupported_concrete_esmiles():
    open_slot = tool_verify_smiles("*C<sep><a>0:R[1]</a>", require_concrete=True)
    assert open_slot["body_valid"] is True
    assert open_slot["valid"] is False
    assert open_slot["esmiles_audit"]["semantic_valid"] is True
    assert open_slot["esmiles_audit"]["open_markush_labels"] == ["R[1]"]

    catalyst = tool_verify_smiles(
        "[*-].[*][Pd+]([*])<sep><a>0:ClO4</a><a>1:NCMe</a><a>3:NCMe</a>",
        require_concrete=True,
    )
    assert catalyst["body_valid"] is True
    assert catalyst["valid"] is False
    assert catalyst["rendering_scope"] == "body_only_partial"
    assert catalyst["esmiles_audit"]["unsupported_labels"] == ["ClO4", "NCMe"]
    assert catalyst["reason"].startswith("Extended-SMILES did not resolve")


def test_verify_smiles_reports_unknown_extension_tags():
    result = tool_verify_smiles("*C<sep><x>0:mystery</x>", require_concrete=True)
    assert result["valid"] is False
    assert result["esmiles_audit"]["unknown_tags"] == ["x"]
