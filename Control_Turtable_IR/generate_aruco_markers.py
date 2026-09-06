#!/usr/bin/env python3
"""
Script d'utilitaire pour générer et sauvegarder les marqueurs ArUco à imprimer.
Génère les marqueurs ID 1 à 6 pour le dictionnaire DICT_4X4_50.
"""
import os
import sys
import cv2

def generate_markers(output_dir="aruco_markers", dict_type=cv2.aruco.DICT_4X4_50, marker_ids=range(1, 7), size_px=600):
    os.makedirs(output_dir, exist_ok=True)
    aruco_dict = cv2.aruco.getPredefinedDictionary(dict_type)
    
    print(f"Génération des marqueurs ArUco (Dictionnaire: DICT_4X4_50, Taille: {size_px}x{size_px}px)...")
    
    for marker_id in marker_ids:
        # Création de l'image du marqueur avec bordure
        if hasattr(cv2.aruco, 'generateImageMarker'):
            marker_img = cv2.aruco.generateImageMarker(aruco_dict, marker_id, size_px)
        else:
            marker_img = cv2.aruco.drawMarker(aruco_dict, marker_id, size_px)
            
        # Ajouter une bordure blanche autour pour faciliter la découpe et la détection
        border_size = 50
        marker_with_border = cv2.copyMakeBorder(
            marker_img, border_size, border_size, border_size, border_size,
            cv2.BORDER_CONSTANT, value=[255, 255, 255]
        )
        
        # Inscrire le numéro d'ID et l'angle théorique sous le marqueur
        angle_deg = (marker_id - 1) * 60.0
        text = f"ArUco ID: {marker_id} ({angle_deg:.0f} deg)"
        font = cv2.FONT_HERSHEY_SIMPLEX
        text_size = cv2.getTextSize(text, font, 0.8, 2)[0]
        
        # Ajouter une zone de texte en bas
        canvas_h = marker_with_border.shape[0] + 60
        canvas_w = marker_with_border.shape[1]
        canvas = 255 * (cv2.imread if False else cv2.copyMakeBorder(
            marker_with_border, 0, 60, 0, 0, cv2.BORDER_CONSTANT, value=[255, 255, 255]
        ))
        
        text_x = (canvas_w - text_size[0]) // 2
        text_y = marker_with_border.shape[0] + 40
        cv2.putText(canvas, text, (text_x, text_y), font, 0.8, (0, 0, 0), 2, cv2.LINE_AA)
        
        filepath = os.path.join(output_dir, f"marker_{marker_id}.png")
        cv2.imwrite(filepath, canvas)
        print(f"  [+] Marqueur ID {marker_id} ({angle_deg:.0f}°) sauvegardé : {filepath}")

    print(f"\nTous les marqueurs ont été sauvegardés dans le dossier '{output_dir}/'.")

if __name__ == "__main__":
    generate_markers()
