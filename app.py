import os
import pandas as pd
from pathlib import Path
from flask import Flask, render_template, request, redirect, url_for, session, flash,send_file
import sqlite3
from functools import wraps
from datetime import timedelta
from dotenv import load_dotenv
from translations.en import TEXTS as EN
from translations.es import TEXTS as ES

base_dir = Path(__file__).resolve().parent
env_path = base_dir / '.env'

#configuarion inicial y seguridad
load_dotenv(dotenv_path=env_path) #cargar variables de entorno

app = Flask(__name__)

app.secret_key = os.getenv("SECRET_KEY")
ADMIN_USER = os.getenv("ADMIN_USER")
ADMIN_PASS = os.getenv("ADMIN_PASS")
app.permanent_session_lifetime = timedelta(minutes=30) #sesion dura 30 minutos

DB_NAME = os.getenv("DB_NAME", "agua.db")

if not app.secret_key or not ADMIN_USER or not ADMIN_PASS:
    raise ValueError(f"❌ ERROR CRÍTICO: No se encontró el archivo .env o faltan variables en {env_path}")

#constantes de negocio (centralizadas para facil modificacion)
LIMITE_M3 = 20
TARIFA_BASE = 5.0
TARIFA_EXCESO = 0.5

def get_texts():
    if session.get("lang") == "es":
        return ES
    return EN  # inglés por defecto

def flash_t(key, category="info", **kwargs):
    """Función para flashear mensajes traducidos automáticamente"""
    texts = get_texts()
    msg = texts.get(key, key) # Si no halla la clave, muestra el texto original
    if kwargs:
        msg = msg.format(**kwargs)
    flash(msg, category)

#funciones de utilidad y logica de negocio
def get_db_connection():
    """Establece conexion con la base de datos."""
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def calcular_pago(inicial, final):
    """calcula el consumo y el total, centraliza la logica de negocio."""
    consumo = final - inicial
    exceso = max(0, consumo - LIMITE_M3)
    total = TARIFA_BASE + (exceso * TARIFA_EXCESO)
    return {
        "consumo": consumo,
        "exceso": exceso,
        "total": total
    }

def init_db():
    """crea tablas si no existen."""
    with get_db_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS usuarios (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cedula TEXT UNIQUE NOT NULL,
                nombre TEXT UNIQUE NOT NULL,
                sector TEXT,
                medidor TEXT UNIQUE,
                celular TEXT,
                correo TEXT,
                creado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        #tabla de lecturas: añadimos el total_pago para que no de error
        conn.execute("""
            CREATE TABLE IF NOT EXISTS lecturas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                usuario_id INTEGER NOT NULL,
                lectura_inicial INTEGER NOT NULL,
                lectura_final INTEGER NOT NULL,
                total_pago REAL,
                estado TEXT DEFAULT 'Pendiente', -- 'Pendiente' o 'Pagado'
                fecha_pago TIMESTAMP,
                creado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (usuario_id) REFERENCES usuarios(id)
            )
        """)
        conn.commit()     

#protector de rutas
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "admin" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated_function

@app.context_processor
def inject_texts():
    return dict(t=get_texts())

@app.route("/lang/<lang>")
def change_lang(lang):
    if lang in ["en", "es"]:
        session["lang"] = lang
    return redirect(request.referrer or url_for("home"))


#rutas principales
@app.route('/')
@login_required
def home():
    with get_db_connection() as conn:
        total_usuarios = conn.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0]
        total_recaudado = conn.execute("SELECT SUM(total_pago) FROM lecturas").fetchone()[0] or 0
        ultimas_lecturas = conn.execute("""
            SELECT u.nombre, l.total_pago
            FROM lecturas l
            JOIN usuarios u ON l.usuario_id = u.id
            ORDER BY l.id DESC LIMIT 5
        """).fetchall()

    return render_template('dashboard.html',
                           total_u=total_usuarios,
                           total_r=total_recaudado,
                           recientes=ultimas_lecturas)

#login
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == "POST":
        user = request.form.get("admin_usuario")
        password = request.form.get("admin_password")

        #contraseňa fija para admin
        if user == os.getenv("ADMIN_USER") and password == os.getenv("ADMIN_PASS"):
            session.permanent = True
            session["admin"] = True
            session["username"] = user
            return redirect(url_for("home"))
        
        flash_t("login_error", "error")
    return render_template("login.html")

#cerrar sesion
@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

#gestiobn ususarios
@app.route('/usuarios')
@login_required
def usuarios():
    #consulta mejora para obtener ultima lectura correctamnete
    query = """
        SELECT u.id, u.cedula, u.nombre, u.sector, u.medidor, u.celular, u.correo,
               l.lectura_final as ultima_lectura, l.creado_en as fecha
        FROM usuarios u
        LEFT JOIN lecturas l ON l.id = (
            SELECT MAX(id) FROM lecturas WHERE usuario_id = u.id
        )
        ORDER BY u.nombre
    """
    with get_db_connection() as conn:
        data = conn.execute(query).fetchall()
    return render_template('usuarios.html', usuarios=data)

#subir usuarios masivvo
@app.route('/usuarios/subir_masivo', methods=['GET', 'POST'])
@login_required
def subir_usuarios_masivo():
    if request.method == 'POST':
        archivo = request.files.get('archivo_socios')
        if not archivo:
            flash_t("select_file", "error")
            return redirect(request.url)

        try:
            df = pd.read_excel(archivo)
            df.columns = df.columns.str.strip().str.lower()

            contador = 0
            with get_db_connection() as conn:
                for _, row in df.iterrows():
                    #limpiamis los datos
                    cedula = str(row['cedula']).strip().split('.')[0].zfill(10)
                    nombre = str(row['nombre']).strip().upper()
                    sector = str(row['sector']).strip() if 'sector' in row else 'General'
                    medidor = str(row['medidor']).strip() if 'medidor' in row else 'SN'
                    correo = str(row['correo']).strip() if 'correo' in row else ''
                    celular = str(row['celular']).strip().split('.')[0] if 'celular' in row else ''

                    existe = conn.execute("SELECT id FROM usuarios WHERE cedula = ?", (cedula,)).fetchone()

                    if not existe:
                        conn.execute("""
                            INSERT INTO usuarios (nombre, cedula, sector, medidor, correo, celular)
                            VALUES (?, ?, ?, ?, ?, ?)
                        """, (nombre, cedula, sector, medidor, correo, celular))
                        contador += 1
                conn.commit()

            flash_t("upload_succes", "success", count=contador)
            return redirect(url_for('usuarios'))
        except Exception as e:
            flash_t("excel_error", "error")
        
    return render_template('subir_usuarios.html')

#crear usuarios 
@app.route('/usuarios/nuevo', methods=['GET', 'POST'])
@login_required
def nuevo_usuario():
    if request.method == 'POST':
        datos = {
            'cedula': request.form.get('cedula', '').strip(),
            'nombre': request.form.get('nombre', '').strip(),
            'sector': request.form.get('sector', '').strip(),
            'medidor': request.form.get('medidor', '').strip(),
            'celular': request.form.get('celular', '').strip(),
            'correo': request.form.get('correo', '').strip()
        }
        

        if not datos['nombre'] or not datos['cedula'] or not datos['medidor']:
            flash_t("fields_required", "error")
        else:
            try:
                with get_db_connection() as conn:
                    conn.execute("""
                        INSERT INTO usuarios (cedula, nombre, sector, medidor, celular, correo)
                        VALUES (?, ?, ?, ?, ?, ?) 
                    """, (datos['cedula'], datos['nombre'], datos['sector'],
                          datos['medidor'], datos['celular'], datos['correo']))
                    conn.commit()
                flash(get_texts()["user_created"], "success")
                return redirect(url_for('usuarios'))
            except sqlite3.IntegrityError:
                flash_t("user_exists", "error")
    return render_template('nuevo_usuario.html')

@app.route('/usuarios/editar/<int:usuario_id>', methods=['GET', 'POST'])
@login_required
def editar_usuario(usuario_id):
    with get_db_connection() as conn:
        usuario = conn.execute("SELECT * FROM usuarios WHERE id = ?", (usuario_id,)).fetchone()

        if request.method == 'POST':
            nombre = request.form.get('nombre', '').strip()
            cedula = request.form.get('cedula', '').strip()
            sector = request.form.get('sector', '').strip()
            medidor = request.form.get('medidor', '').strip()
            celular = request.form.get('celular', '').strip()
            correo = request.form.get('correo', '').strip()

            try:
                conn.execute("""
                    UPDATE usuarios
                    SET nombre = ?, cedula = ?, sector = ?, medidor = ?, celular = ?, correo = ?
                    WHERE id = ?
                """, (nombre, cedula, sector, medidor, celular, correo, usuario_id))
                conn.commit()
                flash_t("user_updated", "success")
                return redirect(url_for('usuarios'))
            except sqlite3.IntegrityError:
                flash_t("user_exists", "error")
    return render_template('editar_usuario.html', u=usuario)    

@app.route('/usuarios/perfil/<int:usuario_id>')
@login_required
def detalle_usuario(usuario_id):
    with get_db_connection() as conn:
        usuario = conn.execute("SELECT * FROM usuarios WHERE id = ?", (usuario_id,)).fetchone()
        
        if not usuario:
            flash("Socio no encontrado", "error")
            return redirect(url_for('usuarios'))

        lects = conn.execute("""
            SELECT id, lectura_inicial, lectura_final, total_pago, estado, creado_en
            FROM lecturas 
            WHERE usuario_id = ? 
            ORDER BY creado_en DESC
        """, (usuario_id,)).fetchall()

        saldo_total = conn.execute("""
            SELECT SUM(total_pago)
            FROM lecturas
            WHERE usuario_id = ? AND estado = 'Pendiente'
        """, (usuario_id,)).fetchone()[0] or 0

    return render_template('perfil_usuario.html', usuario=usuario, lecturas=lects, saldo=saldo_total)

#registrar lecturas
@app.route('/lecturas/nueva', methods=['GET', 'POST'])
@login_required
def nueva_lectura():
    if request.method == 'POST':
        cedula = request.form.get('cedula', '').strip()
        try:
            fin = int(request.form.get('final', 0))

            with get_db_connection() as conn:
                #buscamops al usuario por cedula
                usuario = conn.execute('SELECT id, nombre FROM usuarios WHERE cedula = ?', (cedula,)).fetchone()

                if not usuario:
                    flash_t("user_not_found", "error")
                else:
                    #buscamo automaticamente la ulktima lectura para que sea la inicial
                    ultima = conn.execute("""
                        SELECT lectura_final FROM lecturas
                        WHERE usuario_id = ?
                        ORDER BY creado_en DESC LIMIT 1
                    """, (usuario['id'],)).fetchone()

                    #si no hay uktima kectura la inicial es la misma que la final
                    if not ultima:
                        ini = fin
                        flash_t("first_reading_msg", "info", nombre=usuario['nombre'], valor=fin)
                    else:
                        ini = ultima['lectura_final']

                    if fin < ini:
                        flash_t("reading_error_min", "error", fin=fin, ini=ini)
                    else:
                        #calculamos y guardamos
                        res = calcular_pago(ini, fin)
                        conn.execute("""
                            INSERT INTO lecturas (usuario_id, lectura_inicial, lectura_final, total_pago)
                            values (?, ?, ?, ?)
                        """, (usuario['id'], ini, fin, res['total']))
                        conn.commit()

                        flash_t("reading_saved", "success")
                        #pasamos los datos a la factura 
                        return render_template('factura.html', 
                                               consumo=res['consumo'],
                                               exceso=res['exceso'],
                                               total=res['total'],
                                               nombre=usuario['nombre'],
                                               cedula=cedula,
                                               fecha="Recien generada")
        except (ValueError, TypeError):
            flash_t("invalid_number", "error")

    return render_template('nueva_lectura.html')

@app.route('/historial')
@login_required
def historial():
    with get_db_connection() as conn:
        #consulta mejorada para obtener historial completo
        query = """
            SELECT l.id, u.nombre, l.lectura_inicial, l.lectura_final, l.total_pago, l.creado_en
            FROM lecturas l
            JOIN usuarios u ON l.usuario_id = u.id
            ORDER BY l.creado_en DESC
        """
        data = conn.execute(query).fetchall()
    return render_template('historial.html', lecturas=data)

@app.route('/lecturas/eliminar/<int:lectura_id>')
@login_required
def eliminar_lectura(lectura_id):
    with get_db_connection() as conn:
        conn.execute("DELETE FROM lecturas WHERE id = ?", (lectura_id,))
        conn.commit()
    flash_t("reading_deleted", "success")
    return redirect(url_for('historial'))

@app.route('/factura/<int:lectura_id>')
@login_required
def ver_factura(lectura_id):
    with get_db_connection() as conn:
        #buscamos la lectura y los datos dek usuario al mismo tiempo
        factura = conn.execute("""
            SELECT l.*, u.nombre, u.cedula, u.sector, u.medidor
            FROM lecturas l
            JOIN usuarios u ON l.usuario_id = u.id
            WHERE l.id = ?
        """, (lectura_id,)).fetchone()
    
    if not factura:
        flash("Factura no encontrada", "error")
        return redirect(url_for('historial'))

    consumo_val = factura['lectura_final'] - factura['lectura_inicial']
    exceso_val = max(0, consumo_val - 20)

    return render_template('factura.html',
                           nombre=factura['nombre'],
                           cedula=factura['cedula'],
                           consumo=consumo_val,
                           exceso=exceso_val,
                           total=factura['total_pago'],
                           fecha=factura['creado_en'])

@app.route('/pagar/<int:lectura_id>')
@login_required
def confirmar_pago(lectura_id):
    with get_db_connection() as conn:
        conn.execute("""
            UPDATE lecturas
            SET estado = 'Pagado', fecha_pago = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (lectura_id,))
        conn.commit()
    flash("msg_pay_success", "success")
    return redirect(request.referrer)

@app.route('/calcular')
@login_required
def calcular():
    #esta ruta cargara el simulador o la calculadora de pagos
    return render_template('calcular.html')

@app.route('/reportes')
@login_required
def reportes():
    #aqui puedes mostrar reportes generales
    with get_db_connection() as conn:
        reporte_mensual = conn.execute("""
            SELECT strftime('%m-%Y', creado_en) as mes,
                   COUNT(*) as cantidad,
                   SUM(total_pago) as total
            FROM lecturas
            GROUP BY strftime('%m-%Y', creado_en)
            ORDER BY creado_en DESC
        """).fetchall()
    return render_template('reportes.html', reporte=reporte_mensual)

@app.route('/reportes/deudores')
@login_required
def reporte_deudores():
    with get_db_connection() as conn:
        #buscamos socios que tengan al menos una lectura en esado pendiente
        deudores = conn.execute('''
            SELECT u.id, u.nombre, u.cedula, u.sector,
                   COUNT(l.id) as meses_pendientes,
                   SUM(l.total_pago) as deuda_total
            FROM usuarios u
            JOIN lecturas l ON u.id = l.usuario_id
            WHERE l.estado = 'Pendiente'
            GROUP BY u.id
            ORDER BY deuda_total DESC
        ''').fetchall()

    return render_template('reporte_deudores.html', deudores=deudores)

@app.route('/lecturas/subir_excel', methods=['GET', 'POST'])
@login_required
def subir_excel():
    if request.method == 'POST':
        archivo = request.files.get('archivo_excel')
        if not archivo:
            flash("Por favor selecciona un archivo.", "error")
            return redirect(request.url)

        try:
            df = pd.read_excel(archivo)
            df.columns = df.columns.str.strip().str.lower()

            registros_exitosos = 0
            with get_db_connection() as conn:
                for index, row in df.iterrows():
                    # 1. Obtenemos la cédula del excel como texto limpio
                    c_excel = str(row['cedula']).strip().split('.')[0]
                    # 2. Obtenemos la lectura final del excel
                    l_final = int(row['lectura_final'])

                    # 3. Buscamos al usuario (probamos con y sin cero a la izquierda)
                    usuario = conn.execute(
                        "SELECT id FROM usuarios WHERE cedula = ? OR cedula = ?", 
                        (c_excel, c_excel.zfill(10))
                    ).fetchone()

                    if usuario:
                        u_id = usuario['id']
                        # Buscamos si tiene una lectura anterior
                        ultima = conn.execute(
                            "SELECT lectura_final FROM lecturas WHERE usuario_id = ? ORDER BY id DESC LIMIT 1", 
                            (u_id,)
                        ).fetchone()
                        
                        l_inicial = ultima['lectura_final'] if ultima else l_final

                        # Calculamos el total usando tu función existente
                        resultado = calcular_pago(l_inicial, l_final)
                        
                        # Insertamos la nueva lectura
                        conn.execute("""
                            INSERT INTO lecturas (usuario_id, lectura_inicial, lectura_final, total_pago, estado)
                            VALUES (?, ?, ?, ?, 'Pendiente')
                        """, (u_id, l_inicial, l_final, resultado['total']))
                        
                        registros_exitosos += 1

                conn.commit()

            flash(f"¡Proceso completado! Se cargaron {registros_exitosos} lecturas.", "success")
            return redirect(url_for('historial'))

        except Exception as e:
            flash(f"Error al procesar: {str(e)}", "error")
            
    return render_template('subir_excel.html')

@app.route('/lecturas/descargar_plantilla')
@login_required
def descargar_plantilla():
    with get_db_connection() as conn:
        # SQL avanzado para traer el nombre, cédula y la última lectura registrada
        usuarios = conn.execute('''
            SELECT u.nombre, u.cedula, 
                   COALESCE(l.lectura_final, 0) as lectura_anterior
            FROM usuarios u
            LEFT JOIN (
                SELECT usuario_id, MAX(id) as max_id 
                FROM lecturas GROUP BY usuario_id
            ) last_l ON u.id = last_l.usuario_id
            LEFT JOIN lecturas l ON last_l.max_id = l.id
            ORDER BY u.nombre ASC
        ''').fetchall()

    datos = []
    for u in usuarios:
        datos.append({
            'nombre': u['nombre'],
            'cedula': u['cedula'],
            'lectura_anterior': u['lectura_anterior'], 
            'lectura_final': 0 
        })

    
    df = pd.DataFrame(datos)
    nombre_archivo = "plantilla_lecturas.xlsx"
    ruta_descarga = os.path.join(base_dir, nombre_archivo)
    df.to_excel(ruta_descarga, index=False)

    return send_file(ruta_descarga, as_attachment=True)

if __name__ == '__main__':
    init_db()
    app.run(debug=True, port=5003, host='0.0.0.0')