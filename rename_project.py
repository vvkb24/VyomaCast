import os

replacements = {
    "NewsForge": "VyomaCast",
    "newsforge": "vyomacast",
    "NEWSFORGE": "VYOMACAST"
}

def replace_in_file(filepath):
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
    except Exception as e:
        return False
    
    new_content = content
    for old, new in replacements.items():
        new_content = new_content.replace(old, new)
        
    if new_content != content:
        with open(filepath, 'w', encoding='utf-8', newline='\n') as f:
            f.write(new_content)
        return True
    return False

def main():
    skip_dirs = {'.git', '.venv', 'venv', '__pycache__', 'node_modules', 'graphify-out', '.pytest_cache'}
    updated_files = []
    renamed_files = []
    base_dir = 'e:/news'
    
    # 1. First rename files and directories
    for root, dirs, files in os.walk(base_dir, topdown=False):
        for name in files + dirs:
            if any(skip in os.path.join(root, name).replace("\\", "/") for skip in skip_dirs):
                continue
            new_name = name
            for old, new in replacements.items():
                new_name = new_name.replace(old, new)
            
            if new_name != name:
                old_path = os.path.join(root, name)
                new_path = os.path.join(root, new_name)
                os.rename(old_path, new_path)
                renamed_files.append((old_path, new_path))
                
    # 2. Then replace content in files
    for root, dirs, files in os.walk(base_dir):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for file in files:
            filepath = os.path.join(root, file)
            if file == 'rename_project.py': continue
            if filepath.endswith(('.pyc', '.pyo', '.sqlite', '.db', '.png', '.jpg', '.webp')): continue
            if replace_in_file(filepath):
                updated_files.append(filepath)
                
    print(f"Renamed {len(renamed_files)} files/dirs:")
    for old, new in renamed_files:
        print(f"  {os.path.basename(old)} -> {os.path.basename(new)}")
        
    print(f"Updated {len(updated_files)} files.")

if __name__ == "__main__":
    main()
