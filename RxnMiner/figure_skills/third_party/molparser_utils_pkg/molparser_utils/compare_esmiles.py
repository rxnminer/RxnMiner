# -*- coding: utf-8 -*-
# -----------------------------------------------------------------------------
# Copyright (c) 2026 DPTechnology
# Author: lvhan@dp.tech, wangjunjie@dp.tech
# Created: 2026-01-16
# Description: Contains utilities for molecule comparison
# Version: 1.0
# -----------------------------------------------------------------------------
import re
import os
import sys
import networkx as nx
from rdkit import Chem
from typing import Tuple
from enum import Enum, unique
from dataclasses import dataclass
from rdkit.Chem import DataStructs
from networkx.algorithms import isomorphism
from rdkit.Chem.rdFingerprintGenerator import GetMorganGenerator


# insert root 
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from molparser_utils.translator_to_cxsmiles import Translator
from molparser_utils.smi_to_graph import smiles_to_graph,node_match,edge_match

def calculate_graph_similarity(G1:nx.Graph, G2:nx.Graph)-> float:
    """
    Calculate the similarity between two graphs based on the intersection-over-union (IoU) of their nodes and edges.
    """
    node_mapping = {}
    matched_nodes_G2 = set()  
    for n1 in G1.nodes:
        for n2 in G2.nodes:
            if n2 in matched_nodes_G2:
                continue  #matched, continue
            if node_match(G1.nodes[n1], G2.nodes[n2]):
                node_mapping[n1] = n2
                matched_nodes_G2.add(n2)  #record the matched node
                break  #only matche once
    
    edge_mapping = {}
    matched_edges_G2 = set()
    for e1 in G1.edges:
        for e2 in G2.edges:
            if e2 in matched_edges_G2:
                continue  # matched, continue
            if edge_match(G1.edges[e1],G2.edges[e2]):
                edge_mapping[e1]=e2
                matched_edges_G2.add(e2)    #record the matched edge
                break #only matche once
    
    #IoU
    similarity=(len(node_mapping)+len(edge_mapping))/(len(G1.nodes)+len(G2.nodes)-len(node_mapping)+len(G1.edges)+len(G2.edges)-len(edge_mapping))
    return round(similarity,3)

@unique
class ComparisonState(Enum):
    SAME = 'same_mol'
    DIFF_R = 'mol_with_diff_R_name'
    DIFF_S = 'mol_with_diff_stereo'
    DIFF_B = 'different_backbone'
    INVALID = 'invalid_smiles'

@dataclass(frozen=True)
class ComparisonResult:
    """
    Result container for molecule comparison.

    Attributes
    ----------
    esmiles1 : str
        First input eSMILES string.
    esmiles2 : str
        Second input eSMILES string.
    state : ComparisonState
        Comparison state indicating match type or failure reason.
    similarity_chirality : float
        Similarity score computed with chirality information considered.
    similarity_nochirality : float
        Similarity score computed without considering chirality.
    message : str
        Human-readable message describing the comparison result.
    """

    esmiles1: str
    esmiles2: str
    state: ComparisonState
    similarity_chirality: float
    similarity_nochirality: float
    message: str
    

class MoleculeComparator:
    def __init__(self, Translator):
        #init the cxsmiles Translator
        self.Translator = Translator
        #init the MorganGenerators 
        self.MorganGenerator_Chirality = GetMorganGenerator(radius=2, includeChirality=True, fpSize=1024)
        self.MorganGenerator_NoChirality = GetMorganGenerator(radius=2, includeChirality=False, fpSize=1024)

    def esmiles_to_cxsmiles(self, esmiles: str) -> str:
        """
        Translate ESMILES to canonical CXSMILES.
        """
        out=self.Translator.refactor(esmiles)        
        return getattr(out, "smi", "") or ""
    
    def cxsmiles_to_rdkitmol(self, cxsmiles: str) -> Chem.Mol | None:
        """
        Translate a CXSMILES to an RDKit Mol.
        """
        if not cxsmiles:
            return None
        mol = Chem.MolFromSmiles(cxsmiles)
        return mol

    def rdkitmol_to_cxsmiles(self, mol: Chem.Mol, isomericSmiles: bool = True) -> str:
        """
        Translate an RDKit Mol to canonical CXSMILES.
        
        Args:
            mol: RDKit molecule.
            isomericSmiles: Include stereochemistry information.
        Returns:
            Canonical CXSMILES string.
        """
        return Chem.MolToCXSmiles(mol, canonical=True, isomericSmiles=isomericSmiles)

    def rdkitmol_to_smiles(self, mol: Chem.Mol, isomericSmiles: bool = True) -> str:
        """
        Translate an RDKit Mol to canonical SMILES.
        
        Args:
            mol: RDKit molecule.
            isomericSmiles: Include stereochemistry information.
        Returns:
            Canonical SMILES string.
        """
        return Chem.MolToSmiles(mol, isomericSmiles=isomericSmiles)

    def replace_a_r_tags_with_R(self,esmiles: str) -> str:
        """
        Replace all <a> / <r> tags in an eSMILES string with a literal 'R'.
        """
        pattern = re.compile(r'<[ar]>\d+:[^<]*?</[ar]>')
        return pattern.sub('R', esmiles)
    
    def calculate_similarity(self, mol1: Chem.Mol, mol2: Chem.Mol, isomeric: bool = True)-> float:
        """
        Calculate the Tanimoto similarity between RDKit Mols using Morgan fingerprints.
        """
        if isomeric:
            fp1 = self.MorganGenerator_Chirality.GetFingerprint(mol1)
            fp2 = self.MorganGenerator_Chirality.GetFingerprint(mol2)
        else:
            fp1 = self.MorganGenerator_NoChirality.GetFingerprint(mol1)
            fp2 = self.MorganGenerator_NoChirality.GetFingerprint(mol2)

        similarity = DataStructs.TanimotoSimilarity(fp1, fp2)
        return round(similarity,3)
    
    def calculate_atomLabel_similarity(self, mol1: Chem.Mol, mol2: Chem.Mol, distinguish_R: bool = True) -> float:
        """
        Calculate the Tanimoto similarity between RDKit Mols using Morgan fingerprints.
        Make a distinction between atomLabel and dummyLabel
        """
        #search the different atomLabel or dummyLabel
        diff_atom_id_list=[]
        for i, (a1, a2) in enumerate(zip(mol1.GetAtoms(), mol2.GetAtoms())):

            Props1=a1.GetPropsAsDict()
            Props2=a2.GetPropsAsDict()
            atomLabel_1=Props1.get("atomLabel","")
            atomLabel_2=Props2.get("atomLabel","")
            dummyLabel_1=Props1.get("dummyLabel","")
            dummyLabel_2=Props2.get("dummyLabel","")
            if atomLabel_1 != atomLabel_2:
                diff_atom_id_list.append(i)
            if dummyLabel_1 != dummyLabel_2:
                diff_atom_id_list.append(i)

        rw_mol = Chem.RWMol(mol1)  

        #alter the different atom
        for atom_idx in sorted(diff_atom_id_list, reverse=True):
            atom = rw_mol.GetAtomWithIdx(atom_idx)
            atom.SetChiralTag(Chem.rdchem.ChiralType.CHI_TETRAHEDRAL_CW)

        mol1 = rw_mol.GetMol()
        if distinguish_R:
            fp1 = self.MorganGenerator_Chirality.GetFingerprint(mol1)
            fp2 = self.MorganGenerator_Chirality.GetFingerprint(mol2)
        else:
            fp1 = self.MorganGenerator_NoChirality.GetFingerprint(mol1)
            fp2 = self.MorganGenerator_NoChirality.GetFingerprint(mol2)

        similarity = DataStructs.TanimotoSimilarity(fp1, fp2)
        return round(similarity, 3)
    
    def graphs_match(self, esmiles1: str, esmiles2: str) -> Tuple[bool, int, int]:
        """
        Translate ESMILES to graphs and compare them.
        """
        G1 = smiles_to_graph(esmiles1)
        G2 = smiles_to_graph(esmiles2)
        gm = isomorphism.GraphMatcher(
            G1, G2,
            node_match=node_match,
            edge_match=edge_match
        )
        if gm.is_isomorphic(): #SAME
            return True,1,1
        else:
            graph_similarity=calculate_graph_similarity(G1,G2) 
            return False,graph_similarity,graph_similarity
    
    def repalced_R_match(self, esmiles1: str, esmiles2: str,similarity_chirality: float,similarity_nochirality: float) -> ComparisonResult:
        """
        Match failed, replace <a></a> and <r></r> with R and match again
        Args:
            similarity_chirality: chirality similarity between the original mols (before replaced).
            similarity_nochirality: nochirality similarity between the original mols (before replaced).
        Return:
            An object of ComparisonResult.
        """
        esmiles1=self.replace_a_r_tags_with_R(esmiles1)
        esmiles2=self.replace_a_r_tags_with_R(esmiles2)
        if self.compare(esmiles1, esmiles2, replace_R=True).state.value==ComparisonState.SAME.value:
            return ComparisonResult(esmiles1=esmiles1, esmiles2=esmiles2, state=ComparisonState.DIFF_R,similarity_chirality=similarity_chirality,similarity_nochirality=similarity_nochirality, message="")
        else:
            return ComparisonResult(esmiles1=esmiles1, esmiles2=esmiles2, state=ComparisonState.DIFF_B,similarity_chirality=similarity_chirality,similarity_nochirality=similarity_nochirality, message="")
        
    def compare(self, esmiles1: str, esmiles2: str, replace_R: bool = False) -> ComparisonResult:
        """
        Main function of comparing two ESMILES.
        Args:
            esmiles1: ESMILES of the first molecule to be compared.
            esmiles2: ESMILES of the second molecule to be compared.
            replace_R: if they are replaced <a></a> and <r></r> with R. Default is False.
        Return:
            An object of ComparisonResult.
        """

        #step 1: Molecules with rings compare their graphs
        if "<r>" in esmiles1:
            graphs_match,similarity1,similarity2=self.graphs_match(esmiles1, esmiles2)
            if graphs_match: #SAME in graph comparison
                return ComparisonResult(esmiles1=esmiles1, esmiles2=esmiles2, state=ComparisonState.SAME,similarity_chirality=1,similarity_nochirality=1, message="")
            elif replace_R==False: #different in graph comparison, replaced <a></a> and <r></r> with R and compare again
                return self.repalced_R_match(esmiles1, esmiles2,similarity1, similarity2)
            else: #graph comparison and replaced with R failed
                return ComparisonResult(esmiles1=esmiles1, esmiles2=esmiles2, state=ComparisonState.DIFF_B,similarity_chirality=similarity1,similarity_nochirality=similarity2, message="") 
                
        #Molecules without rings compare molecular directly
        #step 2: esmiles to cxsmiles
        
        cxsmiles1 = self.esmiles_to_cxsmiles(esmiles1)
        cxsmiles2 = self.esmiles_to_cxsmiles(esmiles2)
        
        #step 3: cxsmiles to mol
        mol1 = self.cxsmiles_to_rdkitmol(cxsmiles1)
        mol2 = self.cxsmiles_to_rdkitmol(cxsmiles2)
        
        #step 4: check the validity of mols
        if mol1 is None or mol2 is None:
            return ComparisonResult(esmiles1=esmiles1, esmiles2=esmiles2, state=ComparisonState.INVALID, similarity_chirality=0,similarity_nochirality=0, message=f"{cxsmiles1} or {cxsmiles2} is invalid")
        
        #step 5: mol to canonical cxsmiles
        cano_cxsmiles1 = self.rdkitmol_to_cxsmiles(mol1, isomericSmiles=True)
        cano_cxsmiles2 = self.rdkitmol_to_cxsmiles(mol2, isomericSmiles=True)
        if cano_cxsmiles1 == cano_cxsmiles2: #SAME in 3D space
            return ComparisonResult(esmiles1=esmiles1, esmiles2=esmiles2, state=ComparisonState.SAME, similarity_chirality=1,similarity_nochirality=1, message="")
           
        #There is no longer a possibility of identical molecules. Check if the difference is in 2D space.
        #step 6: ignore ALL stereochemistry (RS + E/Z)
        cano_cxsmiles1_ns = self.rdkitmol_to_smiles(mol1, isomericSmiles=False)
        cano_cxsmiles2_ns = self.rdkitmol_to_smiles(mol2, isomericSmiles=False)
        
        similarity1= self.calculate_atomLabel_similarity(mol1,mol2,distinguish_R=True) #record the similarity in 3D

        if cano_cxsmiles1_ns == cano_cxsmiles2_ns: #SAME in 2D space
            if similarity1==1:
                similarity1=0.98
            return ComparisonResult(esmiles1=esmiles1, esmiles2=esmiles2, state=ComparisonState.DIFF_S, similarity_chirality=similarity1, similarity_nochirality=1, message="")

        if replace_R==False: #different in 3D&2D, try replaced <a></a> and <r></r> with R
            return self.repalced_R_match(esmiles1, esmiles2, similarity1, similarity1)
        
        #step 7: After replaced, they are still different
        similarity2= self.calculate_similarity(mol1,mol2,isomeric=False) #record the similarity in 2D
        return  ComparisonResult(esmiles1=esmiles1, esmiles2=esmiles2,  similarity_chirality=similarity1, similarity_nochirality=similarity2, state=ComparisonState.DIFF_B, message="")
            
    
if __name__=="__main__":

    comparator = MoleculeComparator(Translator)
    esmiles1="*C1C=CC(N2CCNCC2)=CC=1<sep><a>0:R3</a>"
    esmiles2="*C1C=CC(N2CCNCC2)=CC=1<sep><a>0:R[3]</a>"
    #print(Translator.refactor(esmiles1))
    print(Translator.refactor(esmiles2))
    result=comparator.compare(esmiles1, esmiles2)
    
    state=result.state.value
    similarity_chirality=result.similarity_chirality
    similarity_nochirality=result.similarity_nochirality

    print(f"esmiles1: {esmiles1}")
    print(f"esmiles2: {esmiles2}")
    print(f"similarity_chirality:{similarity_chirality}, similarity_nochirality:{similarity_nochirality}")
    print(f"compare result: {state}")
     